from __future__ import annotations

from decimal import Decimal


def test_portfolio_crud_flow(client) -> None:
    create_response = client.post(
        "/api/portfolios",
        json={
            "name": "Main Portfolio",
            "strategy_profile_code": "main",
            "positions": [
                {
                    "quantity": "1.5",
                    "average_entry_price": "45000",
                    "asset": {
                        "symbol": "BTC",
                        "name": "Bitcoin",
                        "coingecko_id": "bitcoin"
                    }
                }
            ],
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()
    portfolio_id = created["id"]

    assert created["positions"][0]["asset"]["symbol"] == "BTC"
    assert created["strategy_profile"]["code"] == "main"

    update_response = client.patch(
        f"/api/portfolios/{portfolio_id}",
        json={"strategy_profile_code": "long_term"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["strategy_profile"]["code"] == "long_term"

    list_response = client.get("/api/portfolios")
    assert list_response.status_code == 200
    assert list_response.json()[0]["position_count"] == 1


def test_csv_preview_endpoint(client) -> None:
    csv_content = (
        "portfolio_name,symbol,quantity,average_entry_price,name\n"
        "Main Portfolio,BTC,1.2,45000,Bitcoin\n"
        "Kids Portfolio,ETH,,2200,Ethereum\n"
    )

    response = client.post(
        "/api/admin/imports/positions/preview",
        files={"file": ("positions.csv", csv_content, "text/csv")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid_row_count"] == 1
    assert payload["invalid_row_count"] == 1


def test_portfolio_accepts_zero_cost_airdrop_position(client) -> None:
    response = client.post(
        "/api/portfolios",
        json={
            "name": "Airdrop Portfolio",
            "strategy_profile_code": "main",
            "positions": [
                {
                    "quantity": "49821",
                    "average_entry_price": "0",
                    "notes": "NOT airdrop",
                    "asset": {
                        "symbol": "NOT",
                        "name": "Notcoin",
                        "coingecko_id": "notcoin",
                    },
                }
            ],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    position = payload["positions"][0]
    assert Decimal(position["average_entry_price"]) == Decimal("0")
    assert Decimal(position["cost_basis"]) == Decimal("0")


def test_dashboard_overview_and_root_page(client) -> None:
    create_response = client.post(
        "/api/portfolios",
        json={
            "name": "Dashboard Portfolio",
            "strategy_profile_code": "main",
            "positions": [
                {
                    "quantity": "2",
                    "average_entry_price": "1500",
                    "asset": {
                        "symbol": "ETH",
                        "name": "Ethereum",
                        "coingecko_id": "ethereum",
                    },
                }
            ],
        },
    )
    assert create_response.status_code == 201

    dashboard_response = client.get("/api/dashboard/overview")
    assert dashboard_response.status_code == 200
    dashboard = dashboard_response.json()

    assert dashboard["totals"]["portfolio_count"] == 1
    assert dashboard["totals"]["position_count"] == 1
    assert dashboard["data_source"].lower().startswith("sqlite")
    assert dashboard["portfolios"][0]["positions"][0]["symbol"] == "ETH"

    root_response = client.get("/")
    assert root_response.status_code == 200
    assert "Portfolio" in root_response.text
