import pytest

from app.llm.pricing import CostBreakdown, ModelPrice, PriceTable, PricingError, calculate_cost
from app.llm.usage import Usage

LUNA = ModelPrice(input_per_million=0.20, output_per_million=1.20, cached_input_per_million=0.02)


def cost(price, inp, out, cached=None):
    return calculate_cost(price, input_tokens=inp, cached_input_tokens=cached, output_tokens=out)


def test_calculation_matches_the_formula():
    price = ModelPrice(2.0, 8.0)
    # 1,500,000 in x $2/M = $3.00 ; 250,000 out x $8/M = $2.00
    result = cost(price, 1_500_000, 250_000)
    assert result.total_cost == pytest.approx(5.0)
    assert (result.input_cost, result.cached_input_cost, result.output_cost) == pytest.approx((3.0, 0.0, 2.0))


def test_small_usage_gives_fractions_of_a_cent():
    assert cost(ModelPrice(1.0, 2.0), 1000, 500).total_cost == pytest.approx(0.002)


def test_known_model_returns_cost():
    table = PriceTable({"m1": ModelPrice(1.0, 3.0)})
    assert table.calculate_cost("m1", 1_000_000, None, 1_000_000).total_cost == pytest.approx(4.0)


def test_unknown_model_returns_none_never_a_guess():
    table = PriceTable({"m1": ModelPrice(1.0, 3.0)})
    assert table.calculate_cost("other-model", 1000, None, 1000) is None
    assert table.cost(None, None, Usage(10, 10, 20)) is None


def test_zero_tokens_cost_zero_for_a_known_model():
    assert cost(ModelPrice(1, 1), 0, 0).total_cost == 0


def test_cached_tokens_are_not_charged_at_the_normal_input_rate():
    # 1000 input of which 400 cached, 200 output  (the example from the spec)
    result = cost(LUNA, 1000, 200, cached=400)
    assert result.input_cost == pytest.approx(600 * 0.20 / 1e6)  # only the NON-cached part
    assert result.cached_input_cost == pytest.approx(400 * 0.02 / 1e6)
    assert result.output_cost == pytest.approx(200 * 1.20 / 1e6)
    assert result.total_cost == pytest.approx(result.input_cost + result.cached_input_cost + result.output_cost)
    assert result.total_cost < cost(LUNA, 1000, 200).total_cost  # cheaper than "no caching"


def test_decimal_arithmetic_has_no_float_noise():
    # 0.1 + 0.2 style noise: 3 tokens at $0.10/M and 3 at $0.20/M must be exactly 0.0000009
    result = cost(ModelPrice(0.10, 0.20), 3, 3)
    assert result.total_cost == 9e-07


def test_cached_tokens_without_a_cached_price_leave_cost_unknown():
    assert cost(ModelPrice(1.0, 2.0), 1000, 10, cached=100) is None  # can't price them; don't guess
    assert cost(ModelPrice(1.0, 2.0), 1000, 10, cached=0).total_cost > 0  # nothing cached: fine


def test_cached_tokens_cannot_exceed_input_tokens():
    result = cost(LUNA, 100, 0, cached=500)  # corrupt report: clamp to input
    assert result.input_cost == 0 and result.cached_input_cost == pytest.approx(100 * 0.02 / 1e6)


def test_load_from_toml_with_optional_cached_price(tmp_path):
    path = tmp_path / "prices.toml"
    path.write_text('[models."a"]\ninput = 0.5\ncached_input = 0.05\noutput = 1.5\n[models."b"]\ninput = 1\noutput = 2\n')
    table = PriceTable.load(path)
    assert table.get("a") == ModelPrice(0.5, 1.5, 0.05)
    assert table.get("b") == ModelPrice(1.0, 2.0, None)


def test_template_file_with_no_models_loads_as_empty(tmp_path):
    path = tmp_path / "prices.toml"
    path.write_text("# only comments\n")
    assert PriceTable.load(path).calculate_cost("anything", 1, None, 1) is None


def test_missing_file_means_all_costs_unknown(tmp_path):
    assert PriceTable.load(tmp_path / "nope.toml").calculate_cost("m", 1, None, 1) is None


@pytest.mark.parametrize(
    "body",
    [
        '[models."m"]\ninput = 1\n',  # missing output
        '[models."m"]\ninput = -1\noutput = 1\n',  # negative
        '[models."m"]\ninput = "1"\noutput = 1\n',  # wrong type
        '[models."m"]\ninput = 1\noutput = 1\ncached_input = -2\n',  # bad cached price
    ],
)
def test_malformed_prices_fail_loudly(tmp_path, body):
    path = tmp_path / "prices.toml"
    path.write_text(body)
    with pytest.raises(PricingError):
        PriceTable.load(path)


def test_shipped_prices_have_the_configured_models_and_no_invented_ones():
    from app.config import DATA_DIR

    table = PriceTable.load(DATA_DIR / "prices.toml")
    assert table.get("gpt-5.6-luna") == ModelPrice(0.20, 1.20, 0.02)
    assert table.get("gpt-4o") is None  # nothing we weren't given
