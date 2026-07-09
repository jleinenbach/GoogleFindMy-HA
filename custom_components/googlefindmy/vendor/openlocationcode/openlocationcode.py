# custom_components/googlefindmy/vendor/openlocationcode/openlocationcode.py
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Vendored from https://github.com/google/open-location-code
# (python/openlocationcode/openlocationcode.py), commit
# dcff1534f70a0d7244d0d1c357c20f0aa28ab355, Apache-2.0.
# Modified from the original: extracted the encode-only path (encode,
# locationToIntegers, encodeIntegers and the constants they require);
# removed decode/shorten/recover/isValid/isShort/isFull/CodeArea helpers and
# the now-unused ``import re``; added type annotations for mypy strict.
# Encoding logic is otherwise unchanged so the output matches upstream and the
# official test vectors.
"""Open Location Code (Plus Code) encoder, vendored (encode-only path).

Plus Codes are short 10-11 character codes usable instead of street addresses.
They are generated offline from latitude/longitude with no API key or network.
See https://github.com/google/open-location-code for the full reference.
"""

import math

# A separator used to break the code into two parts to aid memorability.
SEPARATOR_ = "+"

# The number of characters to place before the separator.
SEPARATOR_POSITION_ = 8

# The character set used to encode the values.
CODE_ALPHABET_ = "23456789CFGHJMPQRVWX"

# The base to use to convert numbers to/from.
ENCODING_BASE_ = len(CODE_ALPHABET_)

# The maximum value for latitude in degrees.
LATITUDE_MAX_ = 90

# The maximum value for longitude in degrees.
LONGITUDE_MAX_ = 180

# The min number of digits to process in a Plus Code.
MIN_DIGIT_COUNT_ = 2

# The max number of digits to process in a Plus Code.
MAX_DIGIT_COUNT_ = 15

# Maximum code length using lat/lng pair encoding. The area of such a
# code is approximately 13x13 meters (at the equator), and should be suitable
# for identifying buildings. This excludes prefix and separator characters.
PAIR_CODE_LENGTH_ = 10

# Inverse of the precision of the pair section of the code.
PAIR_PRECISION_ = ENCODING_BASE_**3

# Number of digits in the grid precision part of the code.
GRID_CODE_LENGTH_ = MAX_DIGIT_COUNT_ - PAIR_CODE_LENGTH_

# Number of columns in the grid refinement method.
GRID_COLUMNS_ = 4

# Number of rows in the grid refinement method.
GRID_ROWS_ = 5

# Multiply latitude by this much to make it a multiple of the finest
# precision.
FINAL_LAT_PRECISION_ = PAIR_PRECISION_ * GRID_ROWS_ ** (
    MAX_DIGIT_COUNT_ - PAIR_CODE_LENGTH_
)

# Multiply longitude by this much to make it a multiple of the finest
# precision.
FINAL_LNG_PRECISION_ = PAIR_PRECISION_ * GRID_COLUMNS_ ** (
    MAX_DIGIT_COUNT_ - PAIR_CODE_LENGTH_
)


def locationToIntegers(latitude: float, longitude: float) -> tuple[int, int]:
    """
    Convert location in degrees into the integer representations.

    This function is exposed for testing purposes and should not be called
    directly.

    Args:
      latitude: Latitude in degrees.
      longitude: Longitude in degrees.
    Return:
      A tuple of the [latitude, longitude] values as integers.
    """
    latVal = int(math.floor(latitude * FINAL_LAT_PRECISION_))
    latVal += LATITUDE_MAX_ * FINAL_LAT_PRECISION_
    if latVal < 0:
        latVal = 0
    elif latVal >= 2 * LATITUDE_MAX_ * FINAL_LAT_PRECISION_:
        latVal = 2 * LATITUDE_MAX_ * FINAL_LAT_PRECISION_ - 1

    lngVal = int(math.floor(longitude * FINAL_LNG_PRECISION_))
    lngVal += LONGITUDE_MAX_ * FINAL_LNG_PRECISION_
    if lngVal < 0:
        # Python's % operator differs from other languages in that it returns
        # the same sign as the divisor. This means we don't need to add the
        # range to the result.
        lngVal = lngVal % (2 * LONGITUDE_MAX_ * FINAL_LNG_PRECISION_)
    elif lngVal >= 2 * LONGITUDE_MAX_ * FINAL_LNG_PRECISION_:
        lngVal = lngVal % (2 * LONGITUDE_MAX_ * FINAL_LNG_PRECISION_)
    return (latVal, lngVal)


def encode(
    latitude: float, longitude: float, codeLength: int = PAIR_CODE_LENGTH_
) -> str:
    """
    Encode a location into an Open Location Code.
    Produces a code of the specified length, or the default length if no length
    is provided.
    The length determines the accuracy of the code. The default length is
    10 characters, returning a code of approximately 13.5x13.5 meters. Longer
    codes represent smaller areas, but lengths > 14 are sub-centimetre and so
    11 or 12 are probably the limit of useful codes.
    Args:
      latitude: A latitude in signed decimal degrees. Will be clipped to the
          range -90 to 90.
      longitude: A longitude in signed decimal degrees. Will be normalised to
          the range -180 to 180.
      codeLength: The number of significant digits in the output code, not
          including any separator characters.
    """
    (latInt, lngInt) = locationToIntegers(latitude, longitude)
    return encodeIntegers(latInt, lngInt, codeLength)


def encodeIntegers(latVal: int, lngVal: int, codeLength: int) -> str:
    """
    Encode a location, as two integer values, into a code.

    This function is exposed for testing purposes and should not be called
    directly.
    """
    if codeLength < MIN_DIGIT_COUNT_ or (
        codeLength < PAIR_CODE_LENGTH_ and codeLength % 2 == 1
    ):
        raise ValueError("Invalid Open Location Code length - " + str(codeLength))
    codeLength = min(codeLength, MAX_DIGIT_COUNT_)
    # Initialise the code string.
    code = ""

    # Compute the grid part of the code if necessary.
    if codeLength > PAIR_CODE_LENGTH_:
        for i in range(0, MAX_DIGIT_COUNT_ - PAIR_CODE_LENGTH_):
            latDigit = latVal % GRID_ROWS_
            lngDigit = lngVal % GRID_COLUMNS_
            ndx = latDigit * GRID_COLUMNS_ + lngDigit
            code = CODE_ALPHABET_[ndx] + code
            latVal //= GRID_ROWS_
            lngVal //= GRID_COLUMNS_
    else:
        latVal //= pow(GRID_ROWS_, GRID_CODE_LENGTH_)
        lngVal //= pow(GRID_COLUMNS_, GRID_CODE_LENGTH_)
    # Compute the pair section of the code.
    for i in range(0, PAIR_CODE_LENGTH_ // 2):
        code = CODE_ALPHABET_[lngVal % ENCODING_BASE_] + code
        code = CODE_ALPHABET_[latVal % ENCODING_BASE_] + code
        latVal //= ENCODING_BASE_
        lngVal //= ENCODING_BASE_

    # Add the separator character.
    code = code[:SEPARATOR_POSITION_] + SEPARATOR_ + code[SEPARATOR_POSITION_:]

    # If we don't need to pad the code, return the requested section.
    if codeLength >= SEPARATOR_POSITION_:
        return code[0 : codeLength + 1]

    # Pad and return the code.
    return code[0:codeLength] + "".zfill(SEPARATOR_POSITION_ - codeLength) + SEPARATOR_
