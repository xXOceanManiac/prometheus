from hello_world import hello


def test_hello_returns_expected_string():
    assert hello() == "Hello, World!"
