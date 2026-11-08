from config.sales import SaleEvent, safe_iso_date, start_date_key, end_date_key


def test_sale_event_calendar_date_helpers_validate_iso_dates():
    assert safe_iso_date("2026-11-19") == "2026-11-19"
    assert safe_iso_date("19.11.2026") is None
    assert safe_iso_date("2026-02-30") is None
    assert start_date_key("black_friday") == "sale.black_friday.start_date"
    assert end_date_key("black_friday") == "sale.black_friday.end_date"


def test_sale_event_can_have_open_ended_campaign_date():
    event = SaleEvent("black_friday", "Чёрная пятница", start_date="2026-11-19")
    assert event.start_date == "2026-11-19"
    assert event.end_date is None
