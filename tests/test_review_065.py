"""0.6.1 第五次复核（R65）的回归测试 + 摘录函数的随机不变量测试。合成数据，不联网。

摘录已经连续三轮被审出边界问题（每轮都是作者手挑的形状没覆盖到），这里另加一组随机测试，
在大量随机正文上检查几条一定要成立的性质，而不是只看几个例子。只用标准库 random，固定种子，可复现。"""

import random
import time

import pytest

from hub.reader import excerpt


# ---------------- 审查者的原样输入 ----------------
@pytest.mark.parametrize("n,limit", [(2201, 2200), (501, 500), (2199, 2200), (499, 500)])
def test_r65_01_oversize_single_term_no_crash(n, limit):
    t, trunc = excerpt("Intro " + "K" * n + " suffix", "K" * n, limit)
    assert len(t) <= limit and trunc


def test_r65_01_oversize_term_with_short_follower():
    t, _ = excerpt("Intro " + "K" * 2201 + " 然后 Nginx reload 用法。" + "后" * 3000, "K" * 2201 + " Nginx", 2200)
    assert "Nginx reload" in t


@pytest.mark.parametrize("limit", [500, 2200])
def test_r65_02_url_query_and_command_kept(limit):
    cmd = "curl -fsSL 'https://deploy.example.test/reload.sh?mode=graceful&wait=30' | sh"
    text = "背" * 100 + "Nginx reload：执行 `" + cmd + "` 以应用新配置" + "后" * 2600
    assert cmd in excerpt(text, "Nginx reload", limit)[0]


def test_r65_02_semicolon_command_kept():
    cmd = "cd /etc/nginx; nginx -t; nginx -s reload"
    text = "背" * 100 + "Nginx reload：执行 " + cmd + " 即可" + "后" * 2600
    assert cmd in excerpt(text, "Nginx reload", 500)[0]


@pytest.mark.parametrize("focus", ["location İstanbul", "location İ", "location Istanbul", "location istanbul"])
def test_r65_03_focus_with_dotted_capital_i(focus):
    text = "x" * 2600 + "İstanbul:ANSWER_TARGET." + "y" * 2400
    assert "ANSWER_TARGET" in excerpt(text, focus, 500)[0]


@pytest.mark.parametrize("focus", ["Java JavaScript", "JavaScript Java"])
def test_r65_04_short_term_does_not_shadow_long(focus):
    text = "Java 资料。" * 80 + "背" * 1000 + "JavaScript 和 Java 区别：ANSWER_COMPARE。" + "后" * 2400
    assert "ANSWER_COMPARE" in excerpt(text, focus, 500)[0]


# ---------------- 随机不变量 ----------------
WORDS = ["Nginx", "reload", "Java", "JavaScript", "re", "İstanbul", "東京", "東京大学", "配置", "生效"]
FILL = ["背景", "无关的文字", "说明", " ", "，", "。", "？", "\n", "https://x.test/a?b=1&c=2", "a;b", "e.g.", "K" * 50]


def _rand_text(rnd: random.Random) -> tuple[str, str, str]:
    """随机正文 + 关注词 + 一段“答案”（含关注词、不含句末标点的完整片段，插在随机位置）。"""
    focus_words = rnd.sample(WORDS, rnd.randint(1, 3))
    answer = " ".join(focus_words) + "：ANSWER_" + str(rnd.randint(1000, 9999)) + " 用 `cmd -x 'u?v=1;w'`"
    pieces = []
    for _ in range(rnd.randint(20, 400)):
        pieces.append(rnd.choice(FILL + WORDS[:3]) * rnd.randint(1, 6))
    pieces.insert(rnd.randint(0, len(pieces)), answer)
    return "".join(pieces), " ".join(focus_words), answer


def _brute_best(hits, limit, n):
    """穷举每个窗口起点 w（窗口 [w, w+limit)），算“不同词数、命中数”的最高分。只用于小输入的对照。"""
    best = (-1, -1)
    for w in range(n):
        ins = [(s, e, k) for s, e, k in hits if s >= w and e <= w + limit]
        best = max(best, (len({k for *_, k in ins}), len(ins)))
    return best


@pytest.mark.parametrize("seed", range(6))
def test_excerpt_invariants(seed):
    """R66-03：只检查函数真正承诺的性质，不再用“抽样看起来唯一”去推断“整段答案必须保留”。
    对每个随机输入：
    1. 不抛异常；输出不超过 limit；正文不超过 limit 时原样返回；
    2. 输出非空，每一段都是原文的连续片段，各段在原文里按顺序出现、互不重叠；
    3. 选中的窗口分数（不同词数、命中数）等于穷举所有窗口得到的最高分；
    4. 选中窗口里的所有命中（核心）完整出现在输出里。"""
    from hub.reader import _focus_terms, _hits, _select
    rnd = random.Random(6500 + seed)
    for _ in range(300):
        text, focus, _answer = _rand_text(rnd)
        limit = rnd.choice([500, 2200])
        out, trunc = excerpt(text, focus, limit)
        assert len(out) <= limit
        if len(text.strip()) <= limit:
            assert (out, trunc) == (text.strip(), False)
            continue
        body = text.strip()
        assert out
        pos = 0
        for part in out.split("\n……\n"):
            i = body.find(part, pos)
            assert i >= 0, part[:60]
            pos = i + len(part)
        hits = _hits(body, _focus_terms(focus), limit)
        if not hits:
            continue
        core_a, core_b, key = _select(hits, limit)
        if len(body) <= 4000:                      # 穷举只在小输入上跑
            assert key == _brute_best(hits, limit, len(body))
        assert body[core_a:core_b] in out


def test_excerpt_keeps_answer_when_it_is_the_only_cluster():
    """R66-03 建议：要求“整段答案保留”时，直接构造无竞争命中、上下文余量足够的输入。"""
    rnd = random.Random(6699)
    for _ in range(300):
        words = rnd.sample(["Nginx", "reload", "Java", "JavaScript", "東京", "配置"], rnd.randint(1, 3))
        answer = " ".join(words) + "：ANSWER_" + str(rnd.randint(1000, 9999)) + " 用 `cmd -x 'u?v=1;w'`"
        limit = rnd.choice([500, 2200])
        pre = "背" * rnd.randint(limit, 3 * limit)
        post = "后" * rnd.randint(limit, 3 * limit)
        text = pre + answer + post
        assert answer in excerpt(text, " ".join(words), limit)[0]


def test_excerpt_linear_time():
    small = "Nginx reload configuration notes. " * 1000
    t0 = time.perf_counter()
    excerpt(small, "Nginx reload", 500)
    a = time.perf_counter() - t0
    t0 = time.perf_counter()
    excerpt(small * 16, "Nginx reload", 500)
    b = time.perf_counter() - t0
    assert b < max(0.05, a * 40)          # 平方增长会是 256 倍
