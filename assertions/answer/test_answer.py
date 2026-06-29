"""E4 assertion test: the executable form of the spec claim "answer() returns 42".

Encodes assertion-spec id `answer-42`. pytest (default prepend import mode) puts
this file's directory on sys.path, so `from answer import answer` resolves to
assertions/answer/answer.py.
"""

from answer import answer


def test_answer():
    assert answer() == 42
