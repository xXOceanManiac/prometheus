from hello_world import greet, hello


def test_hello_returns_expected_string():
    assert hello() == "Hello, World!"


def test_greet_returns_expected_string():
    assert greet() == "Hello, World!"
