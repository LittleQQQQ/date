from app.llm import _invented_numbers

FACTS = "最推薦：先吃飯再看電影；17:40 在河岸影城 101 看《午後的貓》；20:00 到家；每人約 720 元"


def test_summary_using_given_numbers_is_accepted():
    assert not _invented_numbers("17:40 在河岸影城 101 看電影，每人 720 元，20:00 就能到家。", FACTS)


def test_summary_with_invented_numbers_is_rejected():
    assert _invented_numbers("走到影城約需 34 分鐘，每人 720 元。", FACTS) == {"34"}
