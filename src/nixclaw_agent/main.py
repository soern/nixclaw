"""Main entry point for nixclaw-agent."""

import asyncio
import logging
import signal
import sys
from . import prompts
from .config import config
from .db.operations import init_database, DatabaseOperations
from .signal import SignalClient
from .llm import LLMClient, VLLMClient
from .auth import AuthManager
from .context import ContextManager
from .cron import CronScheduler
from .debug_log import configure_debug_logging
from .tools.registry import ToolRegistry
from .tools import register_all_tools
from .tools.cron import set_spawn_subagent_callback, set_stop_activity_callback
from .tools.voice_call import set_end_call_callback
from .core import AgentLoop
from .web.channels import MembershipCache


def _setup_logging() -> None:
    """Configure logging from config."""
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(config.logging.file),
            logging.StreamHandler(sys.stdout),
        ],
    )


async def _run_agent() -> None:
    """Run the agent loop."""
    init_database()

    auth_manager = AuthManager(config.auth)
    db_ops = DatabaseOperations()
    context_manager = ContextManager(config.paths, db_ops)
    cron_scheduler = CronScheduler(config.cron, db_ops)
    tool_registry = ToolRegistry(auth_manager)

    configure_debug_logging(
        mode=config.debug_logging.mode,
        file_path=str(config.debug_logging.file),
        buffer_size=config.debug_logging.buffer_size,
    )
    register_all_tools(tool_registry)

    signal_client = SignalClient(
        api_url=config.signal.api_url,
        phone_number=config.signal.phone_number,
        webhook_port=config.signal.webhook_port,
        webhook_secret=config.signal.webhook_secret,
    )

    llm_client: LLMClient | VLLMClient
    if config.backend == "vllm":
        llm_client = VLLMClient(config.vllm)
        try:
            await llm_client.initialize()
            logging.info(f"Using vLLM backend: {config.vllm.host}/{llm_client.model}")
        except Exception as e:
            # Backend unreachable at startup - don't crash the whole agent over
            # it. llm_client.model stays None, so the first chat() call retries
            # discovery; if the backend is still down that surfaces to the user
            # as a normal "LLM temporarily unavailable" error, same as any other
            # LLM outage, instead of nixclaw failing to start entirely.
            logging.warning(
                f"vLLM backend unreachable at startup ({e}); will retry model "
                "detection on first request"
            )
    else:
        llm_client = LLMClient(config.ollama)
        logging.info(f"Using Ollama backend: {config.ollama.host}/{config.ollama.model}")

    # Signal group membership: used by the unknown-sender gate (core.py) and
    # the web interface. Initial load in the background (signal-api may be slow).
    membership = MembershipCache(config.signal.phone_number, config.signal.api_url,
                                 config.signal.mock_api_url)
    membership_task = asyncio.create_task(membership.refresh())

    agent = AgentLoop(
        signal_client=signal_client,
        llm_client=llm_client,
        auth_manager=auth_manager,
        context_manager=context_manager,
        tool_registry=tool_registry,
        cron_scheduler=cron_scheduler,
        membership=membership,
    )

    set_spawn_subagent_callback(agent.spawn_subagent_task)
    set_stop_activity_callback(agent.cancel_background_activity)

    web_api = None
    if config.webui.enabled:
        from .web.api import start_web_api
        try:
            web_api = await start_web_api(agent, signal_client, auth_manager, membership)
        except Exception as e:
            # The agent keeps serving Signal without the web interface.
            logging.error(f"Web interface API failed to start, continuing without it: {e}")

    call_manager = None
    voice_task = None
    if config.voice_calls.enabled:
        from .voice.manager import AgentHandler, CallManager, ParrotHandler
        handler = AgentHandler(agent) if config.voice_calls.mode == "agent" else ParrotHandler()
        call_manager = CallManager(config.voice_calls, auth_manager, handler)
        set_end_call_callback(call_manager.request_end)
        voice_task = asyncio.create_task(call_manager.run())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(agent.shutdown()))
    loop.add_signal_handler(signal.SIGHUP, lambda: agent.reload("SIGHUP"))

    try:
        await agent.run()
    finally:
        membership_task.cancel()
        if call_manager is not None:
            await call_manager.stop()
        if voice_task is not None:
            voice_task.cancel()
        if web_api is not None:
            await web_api.stop()


def main() -> None:
    """Main entry point - initializes and runs the agent."""
    _setup_logging()
    logging.info("Starting nixclaw-agent")
    try:
        prompts.set_current(prompts.load_prompts())
    except prompts.PromptError as e:
        for err in e.errors:
            logging.error(f"Prompt error: {err}")
        sys.exit(1)
    try:
        asyncio.run(_run_agent())
    except KeyboardInterrupt:
        logging.info("Received keyboard interrupt, shutting down")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()