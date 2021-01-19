import functools
import logging
import random
import string
from asyncio import TimeoutError
from collections import defaultdict
from pathlib import Path
from types import TracebackType
from typing import Callable, Dict, List, Optional, Set, Tuple, Type

import async_timeout
import markovify
from discord import Embed
from discord.ext import commands

from bot.constants import Colours

log = logging.getLogger(__name__)


class RhymeNotFound(commands.CommandError):
    """Raised when a word has no other rhymes."""

    pass


class RhymingSentenceNotFound(commands.CommandError):
    """Raised when a rhyming sentence could not be found."""

    pass


class Cache:
    """
    A storage for word to rhyme sets to reduce API requests.

    Once a rhyme set has been stored or is retrieved, it is also checked to see
    if the poem command should fail immediately to save time.
    """

    cache: Dict[str, Set[str]] = {}

    def __init__(self, word: str, is_instant_fail: bool):
        self.word = word
        self.is_instant_fail = is_instant_fail

    def __enter__(self):
        return self.cache

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType]
    ):
        if len(self.cache[self.word]) == 0:
            logging.info(f"No rhymes were found for the word: {self.word}")
            if self.is_instant_fail:
                raise RhymeNotFound


def memoize(func: Callable) -> Callable:  # Decorator
    """
    A decorator to access and cache rhyme sets.

    If the word to find the rhyme set of already exists in the cache, then
    there is no need to execute `func` because results can be taken from the
    cache. Otherwise, execute `func` and store the results into the cache.
    """
    @functools.wraps(func)
    async def wrapper(
        *args,
        instance: MarkovPoemGenerator = None,
        word: str = None,
        is_instant_fail: bool = None
    ) -> Set[str]:
        with Cache(word, is_instant_fail) as cache:
            if word not in cache:
                logging.info(f"New word cached: {word}")
                cache[word] = await func(instance, word)
            else:
                logging.info(f"Old word used: {word}")

        return cache[word]

    return wrapper


class MarkovPoemGenerator(commands.Cog):
    """
    A cog that provides a poem by taking the markov of a corpus.

    By processing the corpus text through a markov chain, a series of lines
    can be iterated through, whilst corresponding to the given rhyme scheme.
    """

    POEM_TIMEOUT = 20  # In seconds

    SOURCES: List[str] = [
        "shakespeare_corpus.txt"
    ]

    templates: Dict[str, str] = {
        "shakespearean-sonnet": "abab/cdcd/efef/gg"
    }

    rhyme_websites: List[Tuple[bool, str]] = [
        # (is exact rhyme, website link)
        (True, "https://api.datamuse.com/words?rel_rhy="),
        (False, "https://api.datamuse.com/words?rel_nry=")
    ]

    def __init__(self, bot: commands.Bot):
        """Initializes the full corpus text and the markov model."""
        self.bot = bot

        # Load the full text corpus
        for source_file in self.SOURCES:
            with Path(f"bot/resources/valentines/{source_file}").open() as f:
                full_corpus = f.read().splitlines()

        # Create the markov model
        self.model = markovify.Text(full_corpus, state_size=1)

        logging.info("Full text corpus and markov model successfully loaded.")

    @staticmethod
    def _get_last_word(sentence: str) -> str:
        """Returns the last word of a sentence string."""
        if isinstance(sentence, str) is False:
            # Likely to be caused by `make_short_sentence` running out
            # and feeding it None, which is an unlikely scenario
            raise TypeError(f"Argument is a {type(sentence)} instead of a str.")
        return sentence.strip(string.punctuation).split()[-1]

    @memoize
    async def _get_rhyme_set(
        self,
        word: str,
        near_rhyme_min_score: int = 2000
    ) -> None:
        """
        Accesses web APIs to get rhymes and returns a set.

        `near_rhyme_min_score` is to filter out near rhymes that barely rhyme
        with the word. The equivalent for exact rhymes is not necessary as they
        already rhyme.

        Should additional web APIs for rhymes be added, the `min_score` needs
        to be tuned. Perhaps it should be added as an element to the tuple of
        the element of the `self.rhyme_websites` list.
        """
        rhyme_set = set()

        for is_exact, website in self.rhyme_websites:
            min_score = 0 if is_exact else near_rhyme_min_score

            async with self.bot.http_session.get(
                website + word,
                timeout=10
            ) as response:
                curr_set = set(
                    data["word"] for data in await response.json()
                    if data.get("score", 0) >= min_score
                )
                rhyme_set |= curr_set

        return rhyme_set

    async def _get_rhyming_line(
        self,
        word_rhymes: List[str],
        limiter: int = 80000
    ) -> str:
        """
        Returns a sentence string with a last word in `word_rhymes`.

        The function will continue to iterate through sentences provided by the
        markov model, until it finds one that rhymes or until it reaches the
        limiter.
        """
        curr = 0
        line = self.model.make_short_sentence(random.randint(50, 120))
        while self._get_last_word(line) not in word_rhymes:
            if curr >= limiter:
                raise RhymingSentenceNotFound

            line = self.model.make_short_sentence(random.randint(50, 120))
            curr += 1

        return line

    def _get_is_instant_fail(self, scheme: str) -> Dict[str, bool]:
        """
        Checks if a word without rhyme sets should instantly fail.

        If a unit appears more than once but its last word does not have any
        rhyme sets, then it should fail instantly. If not, it would only fail
        when the markov model cannot find a sentence that rhymes, which wastes
        a lot of time.

        It should not fail at all if the unit appears only once as the markov
        model would never need to find a sentence that rhymes with it.
        """
        is_instant_fail: Dict[str, bool] = defaultdict(lambda: False)

        for char in set(scheme):
            if scheme.count(char) >= 2:
                is_instant_fail[char] = True

        return is_instant_fail

    @commands.command()
    async def poem(self, ctx: commands.Context, scheme: str) -> None:
        """
        Gives the user a love poem.

        Poems are often structured by a rhyme scheme, which is often split into
        stanzas. Stanzas are the equivalent of verses in modern pop songs, and
        they are separated by an empty line. The blackslash character indicates
        that an empty line should be generated to create a stanza.

        In this code, a unit is defined by a single character of the scheme. If
        two or more units are the same, they are meant to rhyme (i.e their last
        words rhyme).

        A timeout has been added to the command as a fail-safe measure if
        something freezes.
        """
        # If the `scheme` is actually a template, convert it into a scheme
        scheme = self.templates.get(scheme, scheme)

        # If a sentence does not rhyme and has a rhyming sentence,
        # it would eventually fail.
        is_instant_fail = self._get_is_instant_fail(scheme)

        try:
            async with async_timeout.timeout(self.POEM_TIMEOUT), ctx.typing():
                stanzas = []
                acc_lines = []  # Accumulate lines before joining into a stanza
                rhyme_track = {}  # Maps units to their rhyme sets

                for unit in scheme:
                    # Create new stanza
                    if unit == "/":
                        new_stanza = "\n".join(acc_lines)
                        stanzas.append(new_stanza)
                        acc_lines = []
                        continue

                    # Creating a line for the unit
                    if unit not in rhyme_track:
                        new_line = self.model.make_short_sentence(
                            random.randint(50, 120)
                        )
                        acc_lines.append(new_line)

                        rhyme_track[unit] = await self._get_rhyme_set(
                            instance=self,
                            word=self._get_last_word(new_line),
                            is_instant_fail=is_instant_fail[unit]
                        )
                    else:
                        new_line = await self._get_rhyming_line(
                            word_rhymes=rhyme_track[unit]
                        )
                        acc_lines.append(new_line)

                stanzas.append("\n".join(acc_lines))  # Append final stanza

                poem_embed = Embed(
                    title="A Markov Poem For " + str(ctx.author.name),
                    color=Colours.pink,
                    description="\n\n".join(stanzas)
                )
                await ctx.send(embed=poem_embed)
        except TimeoutError:
            logging.warning("Poem generator timed out.")
            await ctx.send("Unlucky, try again!")
        # except TypeError:
        #     await ctx.send("Type error'd")

    async def cog_command_error(
        self,
        ctx: commands.Context,
        error: Exception
    ) -> None:
        """Handles Discord errors and exceptions."""
        if isinstance(error, RhymeNotFound):
            return await ctx.send("Rhyme impossible.")
        elif isinstance(error, RhymingSentenceNotFound):
            return await ctx.send("Sentence failed.")
        else:
            logging.error(f"Unknown error caught: {error}")


def setup(bot: commands.Bot) -> None:
    """Poem generator cog load."""
    bot.add_cog(MarkovPoemGenerator(bot))