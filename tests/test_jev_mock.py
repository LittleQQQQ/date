from app.jev.mock import audio_language, cuisine, date_fit, screen_type, title_match
from app.jev.questions import CUISINE, DATE_FIT, SCREEN, JevAnswer


def _top(question, probs):
    return question.options[probs.index(max(probs))], max(probs)


def test_probabilities_sum_to_one():
    for probs in (
        date_fit({"name": "x", "description": "燈光昏暗很浪漫", "reviews": []}),
        screen_type({"label": "IMAX 2D 英語"}),
        title_match({"query": "新片 A", "title": "新片A"}),
    ):
        assert abs(sum(probs) - 1) < 1e-9


def test_confident_cases():
    assert _top(SCREEN, screen_type({"label": "IMAX 2D 英語"}))[0] == "IMAX"
    assert _top(SCREEN, screen_type({"label": "4DX 3D 英語"}))[0] == "4DX"
    assert audio_language({"label": "數位 國語配音"})[1] > 0.8
    ctx = {"name": "霧光義式餐館", "description": "燈光昏黃，氣氛安靜浪漫", "reviews": ["很適合約會"]}
    assert _top(DATE_FIT, date_fit(ctx))[0] == "適合約會"
    assert _top(CUISINE, cuisine(ctx | {"description": "義式餐館，手工義大利麵"}))[0] == "義式／西式"


def test_uncertain_cases_have_low_confidence():
    label, conf = _top(SCREEN, screen_type({"label": "TITAN 巨幕 英語"}))
    assert conf < 0.6
    yes = title_match({"query": "新片 A", "title": "新片 A：序章（重映）"})[0]
    assert 0.35 < yes < 0.65


def test_movie_choice_follows_wanted_genre():
    from app.jev.mock import movie_choice

    features = {
        "午後的貓": {"genre": "愛情", "premium": False, "best_buffer": 60},
        "新片 A": {"genre": "動作", "premium": True, "best_buffer": 60},
    }
    options = ("午後的貓", "新片 A")
    action = movie_choice(options, {"_features": features, "_wanted_genre": "動作"})
    assert action[1] > 0.8 and abs(sum(action) - 1) < 1e-9
    romance = movie_choice(options, {"_features": features, "_wanted_genre": "愛情"})
    assert romance[0] > 0.8


def test_diet_ok():
    from app.jev.mock import diet_ok

    spicy = {"name": "湯旅麻辣鍋", "description": "麻辣鍋與個人鍋", "reviews": [], "diet": "不吃辣"}
    assert diet_ok(spicy)[1] > 0.8  # 不行
    veg = {"name": "森之咖啡", "description": "甜點與蔬食輕食", "reviews": [], "diet": "素食"}
    assert diet_ok(veg)[0] > 0.8  # 可以
    meat = {"name": "月見燒肉", "description": "燒肉專門店", "reviews": [], "diet": "素食"}
    assert diet_ok(meat)[1] > 0.8


def test_override_replaces_guess():
    ans = JevAnswer("date_fit:restaurant:1", "q", ["適合約會", "普通", "不適合"], [0.3, 0.4, 0.3])
    fixed = ans.apply_override({"date_fit:restaurant:1": "適合約會"})
    assert fixed.top == "適合約會" and fixed.overridden and fixed.confident(0.99)
    assert ans.apply_override({"date_fit:restaurant:1": "亂填"}) is ans


def test_genre_from_title_and_synopsis():
    from app.jev.mock import genre
    from app.jev.questions import GENRE

    horror = genre({"movie": "鬼入室", "synopsis": "搬進新家後怪事不斷，惡靈附身的驚悚故事。"})
    assert _top(GENRE, horror)[0] == "恐怖" and max(horror) > 0.6
    unknown = genre({"movie": "週末的尼諾", "synopsis": "一個男孩的週末。"})
    assert max(unknown) < 0.6


def test_transit_question_keys():
    from app.jev import transit_choice_question

    go = transit_choice_question("去程", ["高鐵", "台鐵"])
    assert go.key == "transit_go" and go.options == ("高鐵", "台鐵")
    assert go.text == "這次約會的去程，最適合哪一種走法？"
    assert transit_choice_question("回程", ["高鐵"]).key == "transit_back"


def test_transit_choice_weighs_time_against_fare():
    from app.jev.mock import transit_choice

    features = {
        "高鐵": {"saved": 120, "transfers": 0, "fare": 700, "last_margin": None},
        "台鐵": {"saved": 0, "transfers": 0, "fare": 375, "last_margin": None},
    }
    options = ("高鐵", "台鐵")
    free = transit_choice(options, {"_features": features, "_budget_left": 0})
    assert free[0] > 0.75 and abs(sum(free) - 1) < 1e-9  # 沒設預算：省兩小時值得
    tight = transit_choice(options, {"_features": features, "_budget_left": 800})
    assert tight[1] > 0.6  # 高鐵幾乎用光剩下的預算，改搭台鐵


def test_transit_choice_avoids_the_last_train():
    from app.jev.mock import transit_choice

    features = {
        "末班高鐵": {"saved": 10, "transfers": 0, "fare": 700, "last_margin": 5},
        "早一班高鐵": {"saved": 0, "transfers": 0, "fare": 700, "last_margin": 40},
    }
    probs = transit_choice(("末班高鐵", "早一班高鐵"), {"_features": features, "_budget_left": 0})
    assert probs[1] > probs[0]
