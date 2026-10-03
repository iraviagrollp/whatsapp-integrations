"""The small pieces: numbers, templates, configuration."""

import json

import pytest

from wapp import phone, templates
from wapp.config import ConfigError, load_config


@pytest.mark.parametrize("raw", ["9618344433", "09618344433", "+91 96183 44433", "0091-9618344433",
                                 "919618344433", "(961) 834-4433"])
def test_indian_mobiles_written_any_way(raw):
    assert phone.normalise(raw) == "919618344433"


def test_other_countries_and_rubbish():
    assert phone.normalise("+1 555 669 6506") == "15556696506"
    assert phone.normalise("12345") is None
    assert phone.normalise("") is None
    assert phone.display("919618344433") == "+91 96183 44433"


def test_template_slots_in_order_and_named():
    assert templates.slots("{{2}} then {{1}} then {{10}} and {{2}}") == ["1", "2", "10"]
    assert templates.slots("Hi {{customer}}, {{amount}}") == ["customer", "amount"]


def test_a_named_template_carries_the_names():
    template = {"name": "t", "language": "en", "parameter_format": "NAMED",
                "components": [{"type": "BODY", "text": "Hi {{customer}}"}]}
    built = templates.build(template, header_values=[], body_values=["Ravi"])
    assert built["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Ravi", "parameter_name": "customer"}]


def test_config_reads_the_secrets_from_their_files(tmp_path):
    (tmp_path / "token.txt").write_text('  "TOKEN123"  \n', encoding="utf-8")
    (tmp_path / "secret.txt").write_text("\nSECRET\n", encoding="utf-8")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "phone_number_id": "1", "waba_id": "2", "verify_token": "v",
        "token_file": str(tmp_path / "token.txt"), "app_secret_file": str(tmp_path / "secret.txt"),
        "alert_numbers": ["9701491148"], "//comment": "ignored",
    }), encoding="utf-8")
    config = load_config(path)
    assert (config.token, config.app_secret, config.alert_numbers) == ("TOKEN123", "SECRET", ("9701491148",))


def test_config_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("WA_TOKEN", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"phone_number_id": "1", "waba_id": "2"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="verify_token"):
        load_config(path)
    path.write_text(json.dumps({"phone_number_id": "1", "waba_id": "2", "verify_token": "v",
                                "token_file": str(tmp_path / "none.txt")}), encoding="utf-8")
    with pytest.raises(ConfigError, match="No access token"):
        load_config(path)
