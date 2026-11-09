"""0.6.1 第六次复核（R66）的回归测试。合成数据，不联网。"""

import random

import pytest

from hub.reader import _select, excerpt


@pytest.mark.parametrize("focus,word", [("ß SS", "SS"), ("SS ß", "SS"), ("SS ß", "ß"), ("ß SS", "ß"),
                                        ("Straße STRASSE", "STRASSE"), ("STRASSE Straße", "Straße"),
                                        ("ﬃ ffi", "ffi"), ("ffi ﬃ", "ﬃ")])
@pytest.mark.parametrize("limit", [500, 2200])
def test_r66_01_casefold_equal_but_regex_different_terms_both_kept(focus, word, limit):
    """两种拼写 casefold 相同、正则却不认为相等：用户两种都写了，原文里是哪种都要能找到。"""
    text = "背" * 2500 + word + ":ANSWER_X。" + "后" * 2500
    assert "ANSWER_X" in excerpt(text, focus, limit)[0]


def test_r66_02_overlapping_long_hit_does_not_hide_better_window():
    url = "https://docs.example.test/windows?topic=FOO&mode=stable"
    t = list("背" * 3000)
    t[1000:1000 + 14] = list("https:// RT UV")
    t[1465:1465 + len(url)] = list(url)
    text = "".join(t)[:3000]
    out, _ = excerpt(text, "https:// RT UV FOO " + url, 500)
    assert "FOO" in out and "RT" in out and "UV" in out


def test_r66_02_select_matches_brute_force_on_overlapping_hits():
    rnd = random.Random(66)
    for _ in range(3000):
        n, limit = rnd.randint(30, 200), rnd.randint(5, 40)
        hits = set()
        for _ in range(rnd.randint(1, 12)):
            s = rnd.randint(0, n - 2)
            hits.add((s, min(n, s + rnd.randint(1, min(limit, 15))), rnd.randint(0, 4)))
        hits = sorted(h for h in hits if h[1] - h[0] <= limit)
        if not hits:
            continue
        best = (-1, -1)
        for w in range(n):
            ins = [(s, e, k) for s, e, k in hits if s >= w and e <= w + limit]
            best = max(best, (len({k for *_, k in ins}), len(ins)))
        assert _select(hits, limit)[2] == best


def test_r66_03_reviewer_counterexample_is_not_a_failure():
    """审查者的 501 字反例：两个窗口里 [0,500) 分数更高、不含整段答案——这是函数承诺的行为，不是缺陷。"""
    answer = "配置 生效：ANSWER_1234 用 `cmd -x 'u?v=1;w'`"
    text = "配置" + "x" * (501 - 2 - len(answer)) + answer
    out, _ = excerpt(text, "配置 生效", 500)
    assert out.startswith("配置") and "生效" in out



def test_r67_02_select_core_coordinates_and_tie_rule():
    """R67-02：不只比分数，还独立检查核心坐标和同分规则。
    - 核心在原文范围内、长度不超过 limit；
    - 核心里重算出的分数等于返回的分数；
    - 返回的核心 = 按整数窗口穷举时，最早的最优窗口所含命中集合的核心。"""
    rnd = random.Random(67)
    for _ in range(2000):
        n, limit = rnd.randint(30, 200), rnd.randint(5, 40)
        hits = set()
        for _ in range(rnd.randint(1, 12)):
            s = rnd.randint(0, n - 2)
            hits.add((s, min(n, s + rnd.randint(1, min(limit, 15))), rnd.randint(0, 4)))
        hits = sorted(h for h in hits if h[1] - h[0] <= limit)
        if not hits:
            continue
        a, b, key = _select(hits, limit)
        assert 0 <= a < b <= n and b - a <= limit
        ins = [(s, e, k) for s, e, k in hits if a <= s and e <= b]
        assert (len({k for *_, k in ins}), len(ins)) == key
        best, first = (-1, -1), None
        for w in range(n):
            win = [(s, e, k) for s, e, k in hits if s >= w and e <= w + limit]
            sc = (len({k for *_, k in win}), len(win))
            if sc > best:
                best, first = sc, win
        assert (min(s for s, *_ in first), max(e for _, e, _ in first)) == (a, b)


def test_r67_02_hits_match_independent_literal_search():
    """_focus_terms/_hits 用独立的逐字比较（大小写不敏感）对照，免得两边一起错。"""
    from hub.reader import _focus_terms, _hits
    rnd = random.Random(671)
    alpha = "abAB東京 "
    for _ in range(500):
        text = "".join(rnd.choice(alpha) for _ in range(rnd.randint(20, 120)))
        words = ["".join(rnd.choice("abAB東京") for _ in range(rnd.randint(1, 3))) for _ in range(rnd.randint(1, 3))]
        terms = _focus_terms(" ".join(words))
        got = {(s, e) for s, e, _ in _hits(text, terms, 500)}
        want = set()
        for t in terms:
            i = 0
            low, tl = text.lower(), t.lower()
            while True:                      # 非重叠，和 re.finditer 一致
                j = low.find(tl, i)
                if j < 0:
                    break
                want.add((j, j + len(t)))
                i = j + len(t)
        assert got == want, (text, terms)
