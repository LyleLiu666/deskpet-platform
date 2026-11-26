# Vendored unchanged from AgentSpeedProbe Python's protobuf.py.
# MIT License
#
# Copyright (c) 2026 zoefix
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""The validating protobuf wire reader used by the Swift Antigravity source."""


class ProtobufError(ValueError):
    pass


def _varint(data, position):
    result = 0
    for group in range(10):
        if position >= len(data):
            raise ProtobufError("truncated")
        byte = data[position]
        position += 1
        result |= ((byte & 0x7F) << (7 * group)) & ((1 << 64) - 1)
        if not byte & 0x80:
            return result, position
    raise ProtobufError("overlong varint")


class Message:
    def __init__(self, data):
        self.fields = []
        position = 0
        while position < len(data):
            key, position = _varint(data, position)
            number, wire = key >> 3, key & 7
            if number == 0:
                raise ProtobufError("zero field")
            if wire == 0:
                value, position = _varint(data, position)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
                if position + size > len(data):
                    raise ProtobufError("truncated")
                value, position = None, position + size
            elif wire == 2:
                size, position = _varint(data, position)
                if position + size > len(data):
                    raise ProtobufError("truncated")
                value, position = data[position:position + size], position + size
            else:
                raise ProtobufError("unsupported wire type")
            self.fields.append((number, wire, value))

    def _field(self, number, wire, first=False):
        fields = self.fields if first else reversed(self.fields)
        return next((value for name, kind, value in fields if name == number and kind == wire), None)

    def uint(self, number):
        return self._field(number, 0) or 0

    def integer(self, number):
        value = self.uint(number)
        return value - 2 ** 64 if value >= 2 ** 63 else value

    def string(self, number, first=False):
        value = self._field(number, 2, first)
        return value.decode("utf-8", errors="replace") if value is not None else ""

    def message(self, number, first=False):
        value = self._field(number, 2, first)
        return Message(value) if value is not None else None

    def repeated_uint(self, number):
        values = []
        for name, kind, value in self.fields:
            if name != number:
                continue
            if kind == 0:
                values.append(value)
            elif kind == 2:
                position = 0
                while position < len(value):
                    item, position = _varint(value, position)
                    values.append(item)
        return values

    def timestamp_ms(self, number):
        stamp = self.message(number)
        if stamp is None:
            return None
        seconds, nanos = stamp.integer(1), stamp.integer(2)
        if seconds <= 0:
            return None
        value = seconds * 1000 + (nanos // 1_000_000 if 0 <= nanos <= 999_999_999 else 0)
        return value if value <= 2 ** 63 - 1 else None
