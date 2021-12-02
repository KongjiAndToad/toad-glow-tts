# -*- coding: utf-8 -*-

import re

from packaging import version

from TTS.tts.utils.text import cleaners
from TTS.tts.utils.text.chinese_mandarin.phonemizer import chinese_text_to_phonemes
from TTS.tts.utils.text.symbols import _bos, _eos, _punctuations, make_symbols, phonemes, symbols

# pylint: disable=unnecessary-comprehension
# Mappings from symbol to numeric ID and vice versa:
_symbol_to_id = {s: i for i, s in enumerate(symbols)}
_id_to_symbol = {i: s for i, s in enumerate(symbols)}

_phonemes_to_id = {s: i for i, s in enumerate(phonemes)}
_id_to_phonemes = {i: s for i, s in enumerate(phonemes)}

_symbols = symbols
_phonemes = phonemes
# Regular expression matching text enclosed in curly braces:
_CURLY_RE = re.compile(r"(.*?)\{(.+?)\}(.*)")

# Regular expression matching punctuations, ignoring empty space
PHONEME_PUNCTUATION_PATTERN = r"[" + _punctuations.replace(" ", "") + "]+"


def text2phone(text, language):
    """Convert graphemes to phonemes.
    Parameters:
            text (str): text to phonemize
            language (str): language of the text
    Returns:
            ph (str): phonemes as a string seperated by "|"
                    ph = "ɪ|g|ˈ|z|æ|m|p|ə|l"
    """

    # TO REVIEW : How to have a good implementation for this?
    if language == "zh-CN":
        ph = chinese_text_to_phonemes(text)
        return ph
    raise ValueError(f" [!] Language {language} is not supported for phonemization.")


def intersperse(sequence, token):
    result = [token] * (len(sequence) * 2 + 1)
    result[1::2] = sequence
    return result


def pad_with_eos_bos(phoneme_sequence, tp=None):
    # pylint: disable=global-statement
    global _phonemes_to_id, _bos, _eos
    if tp:
        _bos = tp["bos"]
        _eos = tp["eos"]
        _, _phonemes = make_symbols(**tp)
        _phonemes_to_id = {s: i for i, s in enumerate(_phonemes)}

    return [_phonemes_to_id[_bos]] + list(phoneme_sequence) + [_phonemes_to_id[_eos]]


def phoneme_to_sequence(text, cleaner_names, language, enable_eos_bos=False, tp=None, add_blank=False):
    # pylint: disable=global-statement
    global _phonemes_to_id, _phonemes
    if tp:
        _, _phonemes = make_symbols(**tp)
        _phonemes_to_id = {s: i for i, s in enumerate(_phonemes)}

    sequence = []
    clean_text = _clean_text(text, cleaner_names)
    to_phonemes = text2phone(clean_text, language)
    if to_phonemes is None:
        print("!! After phoneme conversion the result is None. -- {} ".format(clean_text))
    # iterate by skipping empty strings - NOTE: might be useful to keep it to have a better intonation.
    for phoneme in filter(None, to_phonemes.split("|")):
        sequence += _phoneme_to_sequence(phoneme)
    # Append EOS char
    if enable_eos_bos:
        sequence = pad_with_eos_bos(sequence, tp=tp)
    if add_blank:
        sequence = intersperse(sequence, len(_phonemes))  # add a blank token (new), whose id number is len(_phonemes)
    return sequence


def sequence_to_phoneme(sequence, tp=None, add_blank=False):
    # pylint: disable=global-statement
    """Converts a sequence of IDs back to a string"""
    global _id_to_phonemes, _phonemes
    if add_blank:
        sequence = list(filter(lambda x: x != len(_phonemes), sequence))
    result = ""
    if tp:
        _, _phonemes = make_symbols(**tp)
        _id_to_phonemes = {i: s for i, s in enumerate(_phonemes)}

    for symbol_id in sequence:
        if symbol_id in _id_to_phonemes:
            s = _id_to_phonemes[symbol_id]
            result += s
    return result.replace("}{", " ")


def text_to_sequence(text, cleaner_names, tp=None, add_blank=False):
    """Converts a string of text to a sequence of IDs corresponding to the symbols in the text.

    The text can optionally have ARPAbet sequences enclosed in curly braces embedded
    in it. For example, "Turn left on {HH AW1 S S T AH0 N} Street."

    Args:
      text: string to convert to a sequence
      cleaner_names: names of the cleaner functions to run the text through
      tp: dictionary of character parameters to use a custom character set.

    Returns:
      List of integers corresponding to the symbols in the text
    """
    # pylint: disable=global-statement
    global _symbol_to_id, _symbols
    if tp:
        _symbols, _ = make_symbols(**tp)
        _symbol_to_id = {s: i for i, s in enumerate(_symbols)}

    sequence = []
    # Check for curly braces and treat their contents as ARPAbet:
    while text:
        m = _CURLY_RE.match(text)
        if not m:
            sequence += _symbols_to_sequence(_clean_text(text, cleaner_names))
            break
        sequence += _symbols_to_sequence(_clean_text(m.group(1), cleaner_names))
        sequence += _arpabet_to_sequence(m.group(2))
        text = m.group(3)

    if add_blank:
        sequence = intersperse(sequence, len(_symbols))  # add a blank token (new), whose id number is len(_symbols)
    return sequence


def sequence_to_text(sequence, tp=None, add_blank=False):
    """Converts a sequence of IDs back to a string"""
    # pylint: disable=global-statement
    global _id_to_symbol, _symbols
    if add_blank:
        sequence = list(filter(lambda x: x != len(_symbols), sequence))

    if tp:
        _symbols, _ = make_symbols(**tp)
        _id_to_symbol = {i: s for i, s in enumerate(_symbols)}

    result = ""
    for symbol_id in sequence:
        if symbol_id in _id_to_symbol:
            s = _id_to_symbol[symbol_id]
            # Enclose ARPAbet back in curly braces:
            if len(s) > 1 and s[0] == "@":
                s = "{%s}" % s[1:]
            result += s
    return result.replace("}{", " ")


def _clean_text(text, cleaner_names):
    for name in cleaner_names:
        cleaner = getattr(cleaners, name)
        if not cleaner:
            raise Exception("Unknown cleaner: %s" % name)
        text = cleaner(text)
    return text


def _symbols_to_sequence(syms):
    return [_symbol_to_id[s] for s in syms if _should_keep_symbol(s)]


def _phoneme_to_sequence(phons):
    return [_phonemes_to_id[s] for s in list(phons) if _should_keep_phoneme(s)]


def _arpabet_to_sequence(text):
    return _symbols_to_sequence(["@" + s for s in text.split()])


def _should_keep_symbol(s):
    return s in _symbol_to_id and s not in ["~", "^", "_"]


def _should_keep_phoneme(p):
    return p in _phonemes_to_id and p not in ["~", "^", "_"]
