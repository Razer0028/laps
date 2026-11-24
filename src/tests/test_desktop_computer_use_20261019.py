"""desktop の Computer Use 流の別名（left_click・coordinate・left_click_drag・hold_key ほか）を読み替える試験。"""

from laps.oc import desktop as d


def setup_function(_):
    d._LAST_LOOK.clear()


def test_left_click_と_coordinate():
    r = d.normalize({"action": "left_click", "coordinate": [100, 200]})
    assert r["action"] == "click" and r["x"] == 100 and r["y"] == 200


def test_triple_click_は回数3():
    r = d.normalize({"action": "triple_click", "coordinate": "[1,2]"})
    assert r["action"] == "click" and r["count"] == 3


def test_scroll_direction_down_は負():
    r = d.normalize({"action": "scroll", "scroll_direction": "down", "scroll_amount": 3})
    assert r["amount"] == -3


def test_hold_key():
    r = d.normalize({"action": "hold_key", "key": "w", "duration": 2})
    assert r["action"] == "key" and r["keys"] == "w" and r["hold"] == 2


def test_left_click_drag():
    r = d.normalize({"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [3, 4]})
    assert r["action"] == "drag"
    assert (r["from_x"], r["from_y"], r["x"], r["y"]) == (1, 2, 3, 4)


def test_写真の後の_coordinate_は写真の座標():
    d._LAST_LOOK.update(left=10, top=20, w=1000, h=500)
    r = d.normalize({"action": "left_click_drag", "start_coordinate": [0, 0], "coordinate": [500, 500]})
    d._from_picture(r, "")
    d._from_picture(r, "from_")
    assert (r["from_x"], r["from_y"]) == (10, 20)
    assert (r["x"], r["y"]) == (510, 270)


def test_zoom_前の写真が無ければ画素():
    r = {"action": "look", "region": "[0, 0, 500, 500]"}
    d._zoom_region(r)
    assert (r["x0"], r["y0"], r["x1"], r["y1"]) == (0, 0, 500, 500)


def test_zoom_前の写真の上の範囲():
    d._LAST_LOOK.update(left=10, top=20, w=1000, h=500)
    r = {"action": "look", "region": [0, 0, 500, 500]}
    d._zoom_region(r)
    assert (r["x0"], r["y0"], r["x1"], r["y1"]) == (10, 20, 510, 270)
