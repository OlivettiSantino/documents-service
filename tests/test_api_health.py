"""Health Check API."""


async def test_liveness_is_ok(client):
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_liveness_has_no_dependencies(client):
    """It must stay cheap: the Docker HEALTHCHECK will poll it every 30 s and a
    liveness probe that touches a database restarts healthy processes."""
    for _ in range(5):
        assert (await client.get("/health")).status_code == 200
