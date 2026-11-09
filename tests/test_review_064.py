"""0.6.1 第四次复核（R64）的回归测试：摘录的计算量、命令完整、Unicode 偏移、长度上限。合成数据，不联网。"""

import random
import time
from pathlib import Path

from hub.reader import excerpt

HERE = Path(__file__).parent


def test_r64_01_many_hits_is_fast():
    """重复关键词的长正文：原来 13.6 万字要 2 秒（命中数平方），会卡住整个插件的事件循环。"""
    body = "Nginx reload configuration notes. " * 4000          # 13.6 万字，8000 个命中
    t0 = time.perf_counter()
    t, _ = excerpt(body, "Nginx reload", 500)
    assert time.perf_counter() - t0 < 0.2 and "Nginx reload" in t


def test_r64_01_work_grows_linearly():
    small = "Nginx reload configuration notes. " * 1000
    big = small * 8
    t0 = time.perf_counter()
    excerpt(small, "Nginx reload", 500)
    a = time.perf_counter() - t0
    t0 = time.perf_counter()
    excerpt(big, "Nginx reload", 500)
    b = time.perf_counter() - t0
    assert b < max(0.05, a * 20)          # 平方增长会是 64 倍


def test_r64_02_command_inside_window_not_cut():
    """审查者的 1486 字样例：命令完整落在原窗口里，原来被句界退缩切成“nginx -s relo”。"""
    body = (HERE / "fixtures_r64_02.txt").read_text(encoding="utf-8")
    assert "nginx -s reload" in body[:500]
    t, _ = excerpt(body, "Nginx reload", 500)
    assert "nginx -s reload" in t and "nginx -s relo" + "\n" not in t


def test_r64_02_hit_word_never_cut_in_half():
    """命中词正好在窗口末尾、窗口里早一点有句号：原来退到句号再补小窗口，把末尾的命中词切成半个。"""
    body = (HERE / "fixtures_r64_02.txt").read_text(encoding="utf-8")
    t, _ = excerpt(body, "Nginx reload", 500)
    assert t.count("relo") == t.count("reload"), t[-30:]
    rnd = random.Random(642)
    for _ in range(200):
        n = rnd.randint(300, 700)
        body = ("说" * (n // 2) + "。" + "说" * (n - n // 2) + "Nginx reload：执行 nginx -s reload"
                + rnd.choice(["。", "", "，"]) + "后" * rnd.randint(200, 1500))
        t, _ = excerpt(body, "reload", 500)
        assert t.count("relo") == t.count("reload"), (n, t[-30:])


def test_r64_03_unicode_lowercase_expansion():
    u = ("İstanbul bilgisi. " * 200) + "Nginx reload 执行 nginx -s reload。" + "乙" * 900
    a = ("Istanbul bilgisi. " * 200) + "Nginx reload 执行 nginx -s reload。" + "乙" * 900
    assert "nginx -s reload" in excerpt(u, "Nginx reload", 500)[0]
    assert "nginx -s reload" in excerpt(a, "Nginx reload", 500)[0]


def test_r64_04_never_longer_than_limit():
    rnd = random.Random(6401)
    for _ in range(240):
        parts = []
        for _ in range(rnd.randint(4, 12)):
            w = rnd.choice(["Nginx", "reload", "背景", "配置", "无关"]) * rnd.randint(1, 30)
            parts.append(w + rnd.choice(["。", "，", " ", ""]))
        t, _ = excerpt("".join(parts) * 3, "Nginx reload", 500)
        assert len(t) <= 500
