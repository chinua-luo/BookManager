from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


WINDOWS_FORBIDDEN_CHARS = set('<>:"/\\|?*')
FIELD_DELIMITERS = (" - ", " _ ")
STRUCTURED_NAME_WITH_EDITION_PATTERN = re.compile(
    r"^(?P<series_number>.+?) - (?P<title>.+?) - (?P<subtitle>.+?) - "
    r"(?P<edition>.+?) _ (?P<authors>.+?)(?P<extension>\.[^.]+)$"
)
STRUCTURED_NAME_WITHOUT_EDITION_PATTERN = re.compile(
    r"^(?P<series_number>.+?) - (?P<title>.+?) - (?P<subtitle>.+?) _ "
    r"(?P<authors>.+?)(?P<extension>\.[^.]+)$"
)
EDITION_LABEL_PATTERN = re.compile(r"^\d+(?:st|nd|rd|th)\s+Edition$", flags=re.I)
CHINESE_EDITION_LABEL_PATTERN = re.compile(r"^第(?P<number>[零一二三四五六七八九十百千]+)版$")
EDITION_LANGUAGES = frozenset({"英文", "中文"})
TITLE_CASE_SMALL_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "from",
        "in",
        "into",
        "nor",
        "of",
        "on",
        "or",
        "over",
        "per",
        "the",
        "to",
        "via",
        "vs",
        "with",
    }
)


@dataclass(frozen=True)
class StructuredFileName:
    series_abbr: str
    number: str
    main_title: str
    subtitle: str
    edition: str
    authors: str
    extension: str
    edition_language: str = "英文"


def normalize_extension(value: str) -> str:
    extension = value.strip()
    if not extension:
        return ""
    if not extension.startswith("."):
        extension = "." + extension
    _validate_optional_field("扩展名", extension[1:], allow_dot=True)
    return extension.lower()


def normalize_structured_file_name(parts: StructuredFileName) -> StructuredFileName:
    """Canonicalize fields before they are persisted or rendered."""
    number = parts.number.strip()
    if number.isdecimal():
        number = number.zfill(3)
    normalized = StructuredFileName(
        series_abbr=parts.series_abbr.strip().upper(),
        number=number,
        main_title=parts.main_title.strip(),
        subtitle=parts.subtitle.strip(),
        edition=parts.edition.strip() or "1",
        authors=parts.authors.strip(),
        extension=normalize_extension(parts.extension),
        edition_language=normalize_edition_language(parts.edition_language),
    )
    values = {
        "系列缩写": normalized.series_abbr,
        "编号": normalized.number,
        "主标题名": normalized.main_title,
        "副标题名": normalized.subtitle,
        "作者信息": normalized.authors,
    }
    for label, value in values.items():
        _validate_optional_field(label, value)
    format_edition(normalized.edition, normalized.edition_language)
    return normalized


def build_structured_file_name(parts: StructuredFileName) -> str:
    normalized = normalize_structured_file_name(parts)
    edition_label = format_edition(normalized.edition, normalized.edition_language)
    series_number = f"{normalized.series_abbr}{normalized.number}"
    leading = " ".join(segment for segment in (series_number, normalized.main_title) if segment)
    segments = [leading, normalized.subtitle, edition_label]
    prefix = " - ".join(segment for segment in segments if segment)
    if not prefix and not normalized.authors:
        raise ValueError("文件名整体不能为空")
    if edition_label:
        _validate_optional_field("版本信息", edition_label)
    if normalized.authors:
        return f"{prefix} _ {normalized.authors}{normalized.extension}" if prefix else f"{normalized.authors}{normalized.extension}"
    return f"{prefix}{normalized.extension}"


def parse_structured_file_name(file_name: str) -> Optional[StructuredFileName]:
    file_name = file_name.strip()
    if not file_name:
        return None
    extension = Path(file_name).suffix
    stem = Path(file_name).stem if extension else file_name
    if not stem.strip():
        return None

    left, authors = split_author_segment(stem)
    segments = [segment.strip() for segment in left.split(" - ") if segment.strip()]
    edition = "1"
    if segments and (EDITION_LABEL_PATTERN.match(segments[-1]) or CHINESE_EDITION_LABEL_PATTERN.match(segments[-1])):
        edition_segment = segments.pop()
        edition = parse_edition_number(edition_segment)
        edition_language = "中文" if CHINESE_EDITION_LABEL_PATTERN.match(edition_segment) else "英文"
    else:
        edition_language = "英文"

    series_abbr = ""
    number = ""
    main_title = ""
    subtitle = ""
    if segments:
        embedded_series, embedded_number, embedded_title = split_leading_series_title(segments[0])
        if embedded_series:
            series_abbr, number, main_title = embedded_series, embedded_number, embedded_title
            segments = segments[1:]
        else:
            candidate_series, candidate_number = split_series_number(segments[0])
            if candidate_number:
                series_abbr, number = candidate_series, candidate_number
                segments = segments[1:]
    if segments and not main_title:
        main_title = segments[0]
        segments = segments[1:]
    if segments:
        subtitle = segments[0]

    return StructuredFileName(
        series_abbr=series_abbr,
        number=number,
        main_title=main_title,
        subtitle=subtitle,
        edition=edition,
        authors=authors,
        extension=extension,
        edition_language=edition_language,
    )


def infer_structured_file_name(file_name: str) -> StructuredFileName:
    parsed = parse_structured_file_name(file_name)
    if parsed is not None:
        return parsed

    path = Path(file_name)
    stem = path.stem.strip()
    extension = path.suffix or ".pdf"
    left, authors = split_author_segment(stem)
    parts = [part.strip() for part in left.split(" - ") if part.strip()]

    series_abbr = ""
    number = ""
    main_title = stem
    subtitle = ""
    edition = "1"

    if parts:
        series_abbr, number = split_series_number(parts[0])
        if number:
            remaining = parts[1:]
        else:
            remaining = parts

        if remaining:
            main_title = remaining[0]
        if len(remaining) >= 2:
            subtitle = remaining[1]
        if len(remaining) >= 3:
            edition = parse_edition_number(remaining[2])
        if not authors and len(remaining) >= 4:
            authors = remaining[3]

    return StructuredFileName(
        series_abbr=series_abbr,
        number=number,
        main_title=main_title,
        subtitle=subtitle,
        edition=edition or "1",
        authors=authors,
        extension=extension,
    )


def validate_structured_file_name(file_name: str) -> None:
    parsed = parse_structured_file_name(file_name)
    if parsed is None:
        raise ValueError("文件名整体不能为空")
    build_structured_file_name(parsed)


def split_series_number(value: str) -> tuple[str, str]:
    text = value.strip()
    match = re.match(r"^(?P<series>.*?)(?P<number>\d+[A-Za-z]?)$", text)
    if not match:
        return text, ""
    return match.group("series"), match.group("number")


def split_leading_series_title(value: str) -> tuple[str, str, str]:
    """Parse the canonical `系列缩写编号 主标题` first segment."""
    series_number, separator, title = value.strip().partition(" ")
    if not separator or not title.strip():
        return "", "", ""
    series_abbr, number = split_series_number(series_number)
    if number:
        return series_abbr, number, title.strip()
    return "", "", ""


def title_case(value: str) -> str:
    """Use title case while leaving articles, conjunctions, and short prepositions lowercase."""
    tokens = re.split(r"(\s+)", value.strip())
    word_indexes = [index for index, token in enumerate(tokens) if token and not token.isspace()]
    if not word_indexes:
        return ""

    first_index = word_indexes[0]
    last_index = word_indexes[-1]
    for index in word_indexes:
        token = tokens[index]
        match = re.fullmatch(r"(?P<prefix>[^A-Za-z]*)(?P<word>[A-Za-z]+)(?P<suffix>[^A-Za-z]*)", token)
        if match and index not in {first_index, last_index} and match.group("word").lower() in TITLE_CASE_SMALL_WORDS:
            tokens[index] = f"{match.group('prefix')}{match.group('word').lower()}{match.group('suffix')}"
            continue
        tokens[index] = re.sub(
            r"[A-Za-z]+(?:['’][sS])?",
            _capitalize_title_word,
            token,
        )
    return "".join(tokens)


def _capitalize_title_word(match: re.Match[str]) -> str:
    word = match.group(0)
    possessive = re.search(r"(?P<apostrophe>['’])[sS]$", word)
    stem = word[: possessive.start()] if possessive else word
    capitalized = stem[:1].upper() + stem[1:].lower()
    return capitalized + (f"{possessive.group('apostrophe')}s" if possessive else "")


def split_author_segment(value: str) -> tuple[str, str]:
    if " _ " not in value:
        return value, ""
    left, authors = value.rsplit(" _ ", 1)
    return left.strip(), authors.strip()


def normalize_edition_language(value: str) -> str:
    language = value.strip()
    if language not in EDITION_LANGUAGES:
        return "英文"
    return language


def format_edition(value: str, language: str = "英文") -> str:
    text = value.strip()
    if not text:
        return ""
    if not text.isdecimal():
        raise ValueError("版本信息必须填写阿拉伯数字")
    number = int(text)
    if number < 1:
        raise ValueError("版本信息必须大于等于 1")
    if number == 1:
        return ""
    if normalize_edition_language(language) == "中文":
        return f"第{chinese_number(number)}版"
    return f"{number}{ordinal_suffix(number)} Edition"


def parse_edition_number(value: str) -> str:
    text = value.strip()
    match = re.match(r"^(?P<number>\d+)(?:st|nd|rd|th)\s+Edition$", text, flags=re.I)
    if match:
        return match.group("number")
    chinese_match = CHINESE_EDITION_LABEL_PATTERN.match(text)
    if chinese_match:
        number = chinese_number_to_int(chinese_match.group("number"))
        return str(number) if number is not None else text
    if text.isdecimal():
        return text
    return text


def ordinal_suffix(number: int) -> str:
    if 10 <= number % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def chinese_number(number: int) -> str:
    digits = "零一二三四五六七八九"
    units = ((1000, "千"), (100, "百"), (10, "十"))
    if number < 10:
        return digits[number]

    result = ""
    remaining = number
    pending_zero = False
    for value, unit in units:
        digit, remaining = divmod(remaining, value)
        if digit:
            if pending_zero:
                result += "零"
                pending_zero = False
            if not (value == 10 and digit == 1 and not result):
                result += digits[digit]
            result += unit
        elif result and remaining:
            pending_zero = True
    if remaining:
        if pending_zero:
            result += "零"
        result += digits[remaining]
    return result


def chinese_number_to_int(value: str) -> int | None:
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    current = 0
    for char in value:
        if char in digits:
            current = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            return None
        total += (current or 1) * unit
        current = 0
    return total + current if total + current > 0 else None


def _validate_field(label: str, value: str, allow_dot: bool = False) -> None:
    text = value.strip()
    if not text:
        raise ValueError(f"{label}不能为空")
    forbidden = WINDOWS_FORBIDDEN_CHARS - ({"."} if allow_dot else set())
    bad_chars = sorted(ch for ch in forbidden if ch in text)
    if bad_chars:
        raise ValueError(f"{label}包含 Windows 文件名不允许的字符：{' '.join(bad_chars)}")
    if not allow_dot:
        for delimiter in FIELD_DELIMITERS:
            if delimiter in text:
                raise ValueError(f"{label}不能包含分隔符 {delimiter!r}")


def _validate_optional_field(label: str, value: str, allow_dot: bool = False) -> None:
    if not value.strip():
        return
    _validate_field(label, value, allow_dot=allow_dot)
