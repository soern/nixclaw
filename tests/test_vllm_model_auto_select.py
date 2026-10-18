"""Tests for vLLM model auto-selection."""
import pytest
from unittest.mock import patch, AsyncMock
import httpx

from src.nixclaw_agent.config import VllmConfig
from src.nixclaw_agent.llm import VLLMClient


class TestVllmConfigFromDict:
    def test_model_none_when_absent(self):
        """When YAML has no model key, config.model should be None."""
        config = VllmConfig.from_dict({"host": "http://localhost:8000"})
        assert config.model is None
        assert config.host == "http://localhost:8000"

    def test_model_preserved_when_present(self):
        """When YAML has explicit model, it should be preserved."""
        config = VllmConfig.from_dict({
            "host": "http://localhost:8000",
            "model": "my-custom-model"
        })
        assert config.model == "my-custom-model"

    def test_model_none_when_empty_dict(self):
        """Empty dict should use defaults (model=None)."""
        config = VllmConfig.from_dict({})
        assert config.model is None

    def test_model_none_when_none(self):
        """None input should use defaults."""
        config = VllmConfig.from_dict(None)
        assert config.model is None


class TestVLLMClientAutoSelect:
    @pytest.fixture
    def mock_response(self):
        """Create a mock httpx response with model data."""
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "data": [{"id": "auto-discovered-model", "object": "model"}]
        }
        resp.raise_for_status = lambda: None
        return resp

    @pytest.mark.asyncio
    async def test_auto_selects_model_when_none(self, mock_response):
        """When model is None, should auto-discover from backend."""
        config = VllmConfig(host="http://localhost:8000", model=None)
        client = VLLMClient(config)

        async_mock = AsyncMock()
        async_mock.get.return_value = mock_response
        async_mock.__aenter__ = AsyncMock(return_value=async_mock)
        async_mock.__aexit__ = AsyncMock(return_value=None)

        with patch("src.nixclaw_agent.llm.httpx.AsyncClient", return_value=async_mock):
            await client.initialize()

        assert client.model == "auto-discovered-model"

    @pytest.mark.asyncio
    async def test_skips_discovery_when_model_set(self):
        """When model is explicitly set, should NOT call backend."""
        config = VllmConfig(host="http://localhost:8000", model="explicit-model")
        client = VLLMClient(config)

        with patch("src.nixclaw_agent.llm.httpx.AsyncClient") as mock_client:
            await client.initialize()
            mock_client.assert_not_called()

        assert client.model == "explicit-model"

    @pytest.mark.asyncio
    async def test_raises_on_connect_error(self):
        """Should raise RuntimeError when backend is unreachable."""
        config = VllmConfig(host="http://localhost:9999", model=None)
        client = VLLMClient(config)

        async_mock = AsyncMock()
        async_mock.get.side_effect = httpx.ConnectError("Connection refused")
        async_mock.__aenter__ = AsyncMock(return_value=async_mock)
        async_mock.__aexit__ = AsyncMock(return_value=None)

        with patch("src.nixclaw_agent.llm.httpx.AsyncClient", return_value=async_mock):
            with pytest.raises(RuntimeError, match="Failed to discover models"):
                await client.initialize()

    @pytest.mark.asyncio
    async def test_raises_on_empty_model_list(self):
        """Should raise RuntimeError when backend returns no models."""
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        resp.raise_for_status = lambda: None

        async_mock = AsyncMock()
        async_mock.get.return_value = resp
        async_mock.__aenter__ = AsyncMock(return_value=async_mock)
        async_mock.__aexit__ = AsyncMock(return_value=None)

        config = VllmConfig(host="http://localhost:8000", model=None)
        client = VLLMClient(config)

        with patch("src.nixclaw_agent.llm.httpx.AsyncClient", return_value=async_mock):
            with pytest.raises(RuntimeError, match="no available models"):
                await client.initialize()

    @pytest.mark.asyncio
    async def test_warns_on_multiple_models(self, mock_response, caplog):
        """Should log warning when multiple models available."""
        mock_response.json.return_value = {
            "data": [
                {"id": "model-a", "object": "model"},
                {"id": "model-b", "object": "model"},
            ]
        }

        async_mock = AsyncMock()
        async_mock.get.return_value = mock_response
        async_mock.__aenter__ = AsyncMock(return_value=async_mock)
        async_mock.__aexit__ = AsyncMock(return_value=None)

        config = VllmConfig(host="http://localhost:8000", model=None)
        client = VLLMClient(config)

        with patch("src.nixclaw_agent.llm.httpx.AsyncClient", return_value=async_mock):
            await client.initialize()

        assert client.model == "model-a"
        assert "2 models" in caplog.text

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self):
        """Should raise RuntimeError on HTTP error from backend."""
        error = httpx.HTTPStatusError(
            "Not Found",
            request=AsyncMock(),
            response=AsyncMock(status_code=404)
        )

        async_mock = AsyncMock()
        async_mock.get.side_effect = error
        async_mock.__aenter__ = AsyncMock(return_value=async_mock)
        async_mock.__aexit__ = AsyncMock(return_value=None)

        config = VllmConfig(host="http://localhost:8000", model=None)
        client = VLLMClient(config)

        with patch("src.nixclaw_agent.llm.httpx.AsyncClient", return_value=async_mock):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                await client.initialize()
