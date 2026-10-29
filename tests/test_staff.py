from nathan.staff import START, Staff, rank_name


def test_elo_rewards_the_better_worker():
    s = Staff()
    s.record("a/m", ok=True, ms=1200)
    s.record("b/m", ok=True, ms=800)
    s.record("c/m", ok=False)
    delta = s.rate({"a/m": 9, "b/m": 6, "c/m": 0})
    assert delta["a/m"] > 0 > delta["c/m"]
    assert abs(sum(delta.values())) < 1e-9  # points move between workers, none appear
    board = s.board()
    assert [r["ref"] for r in board] == ["a/m", "b/m", "c/m"]
    top = board[0]
    assert top["wins"] == 2 and top["avg_score"] == 9 and top["rank"] == rank_name(top["elo"])
    assert board[2]["no_shows"] == 1


def test_upset_earns_more_than_expected_win():
    s = Staff()
    s.db.execute("INSERT INTO staff (ref, elo) VALUES ('pro/m', 1300), ('rookie/m', ?)", (START,))
    upset = s.rate({"rookie/m": 8, "pro/m": 3})["rookie/m"]
    s2 = Staff()
    s2.db.execute("INSERT INTO staff (ref, elo) VALUES ('pro/m', 1300), ('rookie/m', ?)", (START,))
    expected_win = s2.rate({"rookie/m": 3, "pro/m": 8})["pro/m"]
    assert upset > expected_win
