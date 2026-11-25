import unicodedata

from django.core.exceptions import ValidationError


_PHONE_FORMAT_CHARACTERS = frozenset("0123456789+(). -")
_NAME_PUNCTUATION = frozenset(" '-.’")


def validate_person_name(value):
    normalized = unicodedata.normalize("NFC", str(value or "")).strip()
    if not normalized:
        return
    letters = 0
    for character in normalized:
        if character.isalpha():
            letters += 1
        elif character not in _NAME_PUNCTUATION:
            raise ValidationError(
                "Use letters, spaces, apostrophes, periods, or hyphens in names.",
                code="invalid_name",
            )
    if not letters:
        raise ValidationError(
            "Enter a name containing at least one letter.",
            code="invalid_name",
        )


def normalize_philippine_phone_number(value):
    phone = str(value or "").strip()
    if not phone:
        return ""
    if any(character not in _PHONE_FORMAT_CHARACTERS for character in phone):
        raise ValidationError(
            "Enter a Philippine phone number using digits and standard separators.",
            code="invalid_phone",
        )
    if phone.count("+") > 1 or ("+" in phone and not phone.startswith("+")):
        raise ValidationError(
            "Place the country-code plus sign at the beginning of the phone number.",
            code="invalid_phone",
        )

    digits = "".join(character for character in phone if character.isdigit())
    if digits.startswith("63"):
        national_number = digits[2:]
    elif digits.startswith("0"):
        national_number = digits[1:]
    else:
        national_number = digits

    is_mobile = len(national_number) == 10 and national_number.startswith("9")
    is_landline = (
        len(national_number) in {8, 9}
        and national_number[0] in "23456789"
    )
    is_toll_free = len(national_number) == 11 and national_number.startswith("1800")
    if not (is_mobile or is_landline or is_toll_free):
        raise ValidationError(
            "Enter a valid Philippine mobile, landline, or toll-free number.",
            code="invalid_phone",
        )
    return f"+63{national_number}"


def validate_philippine_phone_number(value):
    normalize_philippine_phone_number(value)
