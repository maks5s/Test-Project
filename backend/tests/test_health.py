from httpx import AsyncClient


async def test_health_returns_ok_without_database(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.text == "OK"
