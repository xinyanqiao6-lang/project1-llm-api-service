"""缓存 key 生成逻辑单测：确定性 + 区分度。"""
from app.cache import Cache


def test_make_key_deterministic():
    a = Cache.make_key("m", [{"role": "user", "content": "hi"}], 0.7)
    b = Cache.make_key("m", [{"role": "user", "content": "hi"}], 0.7)
    assert a == b


def test_make_key_differs_by_content():
    a = Cache.make_key("m", [{"role": "user", "content": "hi"}], 0.7)
    b = Cache.make_key("m", [{"role": "user", "content": "hello"}], 0.7)
    assert a != b


def test_make_key_differs_by_temperature():
    a = Cache.make_key("m", [{"role": "user", "content": "hi"}], 0.7)
    b = Cache.make_key("m", [{"role": "user", "content": "hi"}], 0.9)
    assert a != b


def test_make_key_differs_by_model():
    a = Cache.make_key("m1", [{"role": "user", "content": "hi"}], 0.7)
    b = Cache.make_key("m2", [{"role": "user", "content": "hi"}], 0.7)
    assert a != b


def test_make_key_field_order_insensitive():
    # json.dumps(sort_keys=True)：字段顺序不影响 key，保证同类请求能命中缓存
    a = Cache.make_key("m", [{"role": "user", "content": "hi"}], 0.7)
    b = Cache.make_key("m", [{"content": "hi", "role": "user"}], 0.7)
    assert a == b
