#!/usr/bin/env python3
"""Tests for the bash tool: tier-permission enforcement and shell-feature support.

Merged from test_bash_security.py (tier enforcement) and test_bash_shell_features.py
(heredocs, pipes, redirection, and other shell-interpretation features) - both exercised
the same bash() entrypoint from different angles.
"""

import pytest
from src.nixclaw_agent.tools.bash import bash
from src.nixclaw_agent.types import UserTier


class TestBashToolSecurity:
    """Test bash tool security checks."""

    def test_bash_denies_known_user(self):
        """Test that bash tool denies KNOWN tier users."""
        result = bash("echo test", timeout=5, user_tier=UserTier.KNOWN)

        assert result.success is False, "KNOWN user should NOT be able to execute bash"
        assert "privileged" in result.error.lower() or "not available" in result.error.lower(), \
            f"Expected permission error, got: {result.error}"

    def test_bash_denies_unknown_user(self):
        """Test that bash tool denies UNKNOWN tier users."""
        result = bash("echo test", timeout=5, user_tier=UserTier.UNKNOWN)

        assert result.success is False, "UNKNOWN user should NOT be able to execute bash"
        assert "privileged" in result.error.lower() or "not available" in result.error.lower(), \
            f"Expected permission error, got: {result.error}"

    def test_bash_accepts_privileged_user(self):
        """Test that bash tool accepts PRIVILEGED tier users."""
        result = bash("echo test", timeout=5, user_tier=UserTier.PRIVILEGED)

        assert result.success is True, f"PRIVILEGED user should be able to execute bash: {result.error}"

    def test_bash_denies_none_tier(self):
        """Test that bash tool denies None tier (missing tier)."""
        result = bash("echo test", timeout=5, user_tier=None)

        assert result.success is False, "None tier should NOT be able to execute bash"
        assert "privileged" in result.error.lower() or "not available" in result.error.lower(), \
            f"Expected permission error, got: {result.error}"


class TestAuthManager:
    """Test AuthManager tier determination."""

    def test_known_number_returns_known_tier(self):
        """Test that numbers in known_numbers return KNOWN tier."""
        from src.nixclaw_agent.auth import AuthManager
        from src.nixclaw_agent.config import AuthConfig

        config = AuthConfig(
            privileged_numbers=[],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)

        assert auth.get_user_tier("+000001") == UserTier.KNOWN

    def test_privileged_number_returns_privileged_tier(self):
        """Test that numbers in privileged_numbers return PRIVILEGED tier."""
        from src.nixclaw_agent.auth import AuthManager
        from src.nixclaw_agent.config import AuthConfig

        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)

        assert auth.get_user_tier("+000000") == UserTier.PRIVILEGED

    def test_unknown_number_returns_unknown_tier(self):
        """Test that numbers not in any list return UNKNOWN tier."""
        from src.nixclaw_agent.auth import AuthManager
        from src.nixclaw_agent.config import AuthConfig

        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)

        assert auth.get_user_tier("+999999") == UserTier.UNKNOWN

    def test_number_in_both_lists_returns_privileged(self):
        """Test that if number is in both lists, PRIVILEGED takes precedence."""
        from src.nixclaw_agent.auth import AuthManager
        from src.nixclaw_agent.config import AuthConfig

        config = AuthConfig(
            privileged_numbers=["+000001"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)

        assert auth.get_user_tier("+000001") == UserTier.PRIVILEGED


class TestToolRegistryExecuteTool:
    """Test ToolRegistry.execute_tool permission enforcement."""

    @pytest.mark.asyncio
    async def test_execute_tool_denies_known_user_for_bash(self):
        """Test that execute_tool denies KNOWN user for bash tool."""
        from src.nixclaw_agent.auth import AuthManager
        from src.nixclaw_agent.config import AuthConfig
        from src.nixclaw_agent.tools.registry import ToolRegistry
        from src.nixclaw_agent.tools import register_all_tools

        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)
        registry = ToolRegistry(auth)
        register_all_tools(registry)

        result = await registry.execute_tool("bash", {"cmd": "echo test"}, UserTier.KNOWN)

        assert result.success is False, "KNOWN user should NOT be able to execute bash via registry"
        assert "not available" in result.error.lower() or "privileged" in result.error.lower(), \
            f"Expected permission error, got: {result.error}"

    @pytest.mark.asyncio
    async def test_execute_tool_allows_privileged_user_for_bash(self):
        """Test that execute_tool allows PRIVILEGED user for bash tool."""
        from src.nixclaw_agent.auth import AuthManager
        from src.nixclaw_agent.config import AuthConfig
        from src.nixclaw_agent.tools.registry import ToolRegistry
        from src.nixclaw_agent.tools import register_all_tools

        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)
        registry = ToolRegistry(auth)
        register_all_tools(registry)

        result = await registry.execute_tool("bash", {"cmd": "echo test"}, UserTier.PRIVILEGED)

        assert result.success is True, f"PRIVILEGED user should be able to execute bash via registry: {result.error}"


class TestBashToolHeredoc:
    """Test heredoc support in bash tool."""

    def test_heredoc_basic(self):
        """Test basic heredoc functionality - write file using heredoc."""
        result = bash(
            "cat << 'EOF'\nHello World\nThis is a test\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Heredoc failed: {result.error}"
        assert "Hello World" in result.output
        assert "This is a test" in result.output

    def test_heredoc_with_variable(self):
        """Test heredoc with shell variable."""
        result = bash(
            "cat << 'EOF'\nPATH=$PATH:/custom\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Heredoc with var failed: {result.error}"
        assert "PATH=" in result.output

    def test_heredoc_quoted_delimiter(self):
        """Test heredoc with quoted delimiter preserves literal content."""
        result = bash(
            "cat << 'END'\n$HOME is literal\nEND",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True
        assert "$HOME is literal" in result.output

    def test_heredoc_file_write(self):
        """Test using heredoc to write a file (multi-line content)."""
        result = bash(
            "cat > /tmp/heredoc_test.txt << 'EOF'\nLine 1\nLine 2\nLine 3\nEOF\ncat /tmp/heredoc_test.txt",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Heredoc file write failed: {result.error}"
        assert "Line 1" in result.output
        assert "Line 2" in result.output
        assert "Line 3" in result.output

    def test_heredoc_empty_content(self):
        """Test heredoc with empty content."""
        result = bash(
            "cat << 'EOF'\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True

    def test_heredoc_multiple_heredocs(self):
        """Test multiple heredocs in one command."""
        result = bash(
            "cat << 'EOF1'\nFirst file\nEOF1\ncat << 'EOF2'\nSecond file\nEOF2",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Multiple heredocs failed: {result.error}"
        assert "First file" in result.output
        assert "Second file" in result.output

    def test_heredoc_special_characters_backticks(self):
        """Test heredoc containing backticks."""
        result = bash(
            "cat << 'EOF'\necho `ls`\n`whoami`\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Backticks in heredoc failed: {result.error}"
        assert "echo" in result.output.lower() or "root" in result.output.lower()

    def test_heredoc_with_newlines_and_tabs(self):
        """Test heredoc with explicit newlines and tabs."""
        result = bash(
            "cat << 'EOF'\nLine1\\nLine2\nTab:\\tHere\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Escape sequences in heredoc failed: {result.error}"

    def test_heredoc_unicode_content(self):
        """Test heredoc with unicode content."""
        result = bash(
            "cat << 'EOF'\nUnicode: éèê\nEmoji: \U0001F600\U0001F4BB\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Unicode heredoc failed: {result.error}"

    def test_heredoc_binary_like_content(self):
        """Test heredoc with binary-like content (control chars only, no nulls).

        Note: Null bytes (\x00) cause "embedded null byte" error in Python subprocess
        because they are used for internal string handling. This is expected behavior.
        """
        result = bash(
            "cat << 'EOF'\nBinary content test\nControl: \x7f\nOther chars: \x01\x02\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Non-null binary-like heredoc failed: {result.error}"
        assert "Binary content test" in result.output

    def test_heredoc_unquoted_delimiter_expands(self):
        """Test heredoc with unquoted delimiter expands variables."""
        result = bash(
            "VAR=expanded && cat << EOF\nValue: $VAR\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True
        assert "expanded" in result.output

    def test_heredoc_very_long_content(self):
        """Test heredoc with very long content (simulate large file write)."""
        long_line = "x" * 1000
        result = bash(
            f"cat << 'EOF'\n{long_line}\nEOF",
            timeout=10,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Long heredoc failed: {result.error}"
        assert long_line in result.output


class TestBashToolRedirection:
    """Test output/input redirection in bash tool."""

    def test_output_redirection(self):
        """Test output redirection to file."""
        result = bash(
            "echo 'test content' > /tmp/redirect_test.txt && cat /tmp/redirect_test.txt",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Redirection failed: {result.error}"
        assert "test content" in result.output

    def test_append_redirection(self):
        """Test append redirection."""
        result = bash(
            "echo 'first' > /tmp/append_test.txt && echo 'second' >> /tmp/append_test.txt && cat /tmp/append_test.txt",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Append redirection failed: {result.error}"
        assert "first" in result.output
        assert "second" in result.output

    def test_input_redirection(self):
        """Test input redirection from file."""
        result = bash(
            "echo 'input test' > /tmp/input_file.txt && wc -l < /tmp/input_file.txt",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Input redirection failed: {result.error}"
        assert "1" in result.output

    def test_redirection_with_glob(self):
        """Test redirection combined with glob."""
        result = bash(
            "echo 'glob test' > /tmp/glob_test_1.txt && cat /tmp/glob_test_1.txt",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Redirection with glob failed: {result.error}"

    def test_double_redirection(self):
        """Test redirecting stderr and stdout."""
        result = bash(
            "echo 'stdout' && echo 'stderr' >&2",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True
        assert "stdout" in result.output

    def test_redirection_to_dev_null(self):
        """Test redirecting output to /dev/null."""
        result = bash(
            "echo 'hidden' > /dev/null && echo 'visible'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True
        assert "visible" in result.output
        assert "hidden" not in result.output


class TestBashToolPipes:
    """Test pipe support in bash tool."""

    def test_simple_pipe(self):
        """Test simple pipe between two commands."""
        result = bash(
            "echo 'hello world' | wc -w",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Pipe failed: {result.error}"
        assert "2" in result.output.strip()

    def test_pipe_chain(self):
        """Test chain of pipes."""
        result = bash(
            "echo 'one two three four' | tr ' ' '\\n' | wc -l",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Pipe chain failed: {result.error}"
        assert "4" in result.output.strip()

    def test_pipe_with_grep(self):
        """Test pipe with grep."""
        result = bash(
            "echo -e 'apple\\nbanana\\ncherry' | grep 'an'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Pipe with grep failed: {result.error}"
        assert "banana" in result.output

    def test_pipe_stderr(self):
        """Test piping stderr."""
        result = bash(
            "cat nonexistent 2>&1 | grep 'No such'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Stderr pipe failed: {result.error}"
        assert "No such" in result.output or "exist" in result.output.lower()

    def test_pipe_with_subshell(self):
        """Test pipe involving subshell."""
        result = bash(
            "(echo 'subshell') | cat",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Subshell pipe failed: {result.error}"
        assert "subshell" in result.output


class TestBashToolCommandSubstitution:
    """Test command substitution in bash tool."""

    def test_command_substitution_dollar(self):
        """Test $(...) command substitution."""
        result = bash(
            "echo \"Current directory: $(pwd)\"",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Command substitution failed: {result.error}"
        assert "Current directory:" in result.output

    def test_command_substitution_backticks(self):
        """Test backtick command substitution."""
        result = bash(
            "echo \"User: `whoami`\"",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Backtick substitution failed: {result.error}"
        assert "User:" in result.output

    def test_nested_command_substitution(self):
        """Test nested command substitution."""
        result = bash(
            "echo $(echo $(echo nested))",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Nested substitution failed: {result.error}"
        assert "nested" in result.output

    def test_command_substitution_with_quotes(self):
        """Test command substitution containing quotes."""
        result = bash(
            "echo $(echo \"it's a test\")",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Quoted substitution failed: {result.error}"
        assert "it's a test" in result.output or "it" in result.output


class TestBashToolEnvironmentVariables:
    """Test environment variable support in bash tool."""

    def test_env_variable_expansion(self):
        """Test $VAR environment variable expansion."""
        result = bash(
            "echo $HOME",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Env var failed: {result.error}"
        assert result.output.strip() != ""

    def test_env_variable_in_string(self):
        """Test environment variable in double-quoted string."""
        result = bash(
            'echo "PATH has $PATH"',
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Env var in string failed: {result.error}"
        assert "PATH has" in result.output

    def test_env_variable_assignment(self):
        """Test setting an environment variable."""
        result = bash(
            "MYVAR=test_value && echo $MYVAR",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Env assignment failed: {result.error}"
        assert "test_value" in result.output

    def test_env_variable_with_special_chars(self):
        """Test env var containing special characters."""
        result = bash(
            "MYVAR='value with spaces and $pecial' && echo \"$MYVAR\"",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Special chars in var failed: {result.error}"
        assert "value with spaces" in result.output

    def test_env_variable_concatenation(self):
        """Test env var concatenation."""
        result = bash(
            "VAR1=hello && VAR2=world && echo $VAR1$VAR2",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Var concatenation failed: {result.error}"
        assert "helloworld" in result.output


class TestBashToolComplexNesting:
    """Test complex nested shell features."""

    def test_heredoc_with_command_substitution(self):
        """Test heredoc containing command substitution."""
        result = bash(
            "cat << 'EOF'\nCurrent user: `whoami`\nCurrent dir: $(pwd)\nEOF",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Heredoc with cmd sub failed: {result.error}"

    def test_pipe_with_redirection(self):
        """Test pipe combined with redirection."""
        result = bash(
            "echo 'line1\\nline2\\nline3' | grep 'line' > /tmp/grep_output.txt && cat /tmp/grep_output.txt",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Pipe with redirect failed: {result.error}"
        assert "line1" in result.output or "line2" in result.output or "line3" in result.output

    def test_subshell_with_redirection(self):
        """Test subshell with redirection."""
        result = bash(
            "(echo 'subshell output') > /tmp/subshell_test.txt && cat /tmp/subshell_test.txt",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Subshell redirect failed: {result.error}"
        assert "subshell output" in result.output

    def test_conditional_with_command(self):
        """Test && and || operators."""
        result = bash(
            "true && echo 'and then' || echo 'else'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True
        assert "and then" in result.output
        assert "else" not in result.output

    def test_negation_operator(self):
        """Test ! negation operator."""
        result = bash(
            "! false && echo 'negation works'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Negation failed: {result.error}"
        assert "negation works" in result.output


class TestBashToolSpecialCharacters:
    """Test handling of special characters in commands."""

    def test_nested_single_double_quotes(self):
        """Test command with nested single and double quotes."""
        result = bash(
            "echo \"it's a 'test' with \\\"quotes\\\"\"",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Nested quotes failed: {result.error}"

    def test_backslash_escapes(self):
        """Test backslash escape sequences."""
        result = bash(
            "echo 'line1\\nline2' && echo 'tabs\\there'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Backslash escapes failed: {result.error}"

    def test_dollar_sign_literal(self):
        """Test escaping $ to get literal $."""
        result = bash(
            r"echo 'Literal $100'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True
        assert "Literal $100" in result.output

    def test_backslash_at_end_of_line(self):
        """Test backslash at end of line (line continuation)."""
        result = bash(
            "echo 'first line' \\\n&& echo 'second line'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Line continuation failed: {result.error}"
        assert "first line" in result.output
        assert "second line" in result.output


class TestBashToolPreservesExistingBehavior:
    """Test that existing simple commands still work."""

    def test_simple_echo(self):
        """Test simple echo still works."""
        result = bash(
            "echo 'hello'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Simple echo failed: {result.error}"
        assert "hello" in result.output

    def test_command_with_args(self):
        """Test command with multiple arguments."""
        result = bash(
            "ls -la /tmp",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True, f"Command with args failed: {result.error}"

    def test_empty_command_rejected(self):
        """Test that empty command is still rejected."""
        result = bash("", timeout=5, user_tier=UserTier.PRIVILEGED)

        assert result.success is False
        assert "empty" in result.error.lower() or "invalid" in result.error.lower()

    def test_whitespace_only_command_rejected(self):
        """Test that whitespace-only command is rejected."""
        result = bash("   ", timeout=5, user_tier=UserTier.PRIVILEGED)

        assert result.success is False

    def test_privileged_still_required(self):
        """Test that privilege check still enforced."""
        result = bash("echo test", timeout=5, user_tier=UserTier.KNOWN)
        assert result.success is False

        result = bash("echo test", timeout=5, user_tier=UserTier.UNKNOWN)
        assert result.success is False


class TestBashToolErrorHandling:
    """Test error handling still works with shell features."""

    def test_nonexistent_command_with_shell_feature(self):
        """Test error when command fails in shell context."""
        result = bash(
            "nonexistent_cmd_12345 || echo 'failed as expected'",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is True
        assert "failed as expected" in result.output

    def test_timeout_still_works(self):
        """Test that timeout still works with shell commands."""
        result = bash(
            "sleep 10",
            timeout=1,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is False
        assert "timed out" in result.error.lower()

    def test_syntax_error_in_shell(self):
        """Test that shell syntax errors are caught."""
        result = bash(
            "if then fi",
            timeout=5,
            user_tier=UserTier.PRIVILEGED
        )

        assert result.success is False
        assert result.error is not None


def _pid_alive(pid: int) -> bool:
    """True if pid exists and is not a zombie (a killed orphan may linger unreaped)."""
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line.split()[1]
    except FileNotFoundError:
        return False
    return True


class TestBashToolOutputAndTimeout:
    """Separate stdout/stderr pipes, partial output and process-group kill on timeout."""

    def test_stdout_and_stderr_both_returned(self):
        result = bash("echo to-stdout; echo to-stderr >&2", timeout=5, user_tier=UserTier.PRIVILEGED)

        assert result.success is True
        assert "to-stdout" in result.output
        assert "to-stderr" in result.output

    def test_stderr_only_on_failure_kept_in_output(self):
        result = bash("echo boom >&2; exit 4", timeout=5, user_tier=UserTier.PRIVILEGED)

        assert result.success is False
        assert result.error == "Exit code: 4\nboom\n"
        assert "boom" in result.output

    def test_nonzero_exit_without_output_keeps_plain_error(self):
        result = bash("exit 3", timeout=5, user_tier=UserTier.PRIVILEGED)

        assert result.success is False
        assert result.error == "Exit code: 3"

    def test_nonzero_exit_output_reaches_model(self):
        from src.nixclaw_agent.llm import _tool_result_content
        result = bash("echo 'rejected: copies'; exit 1", timeout=5, user_tier=UserTier.PRIVILEGED)

        assert _tool_result_content(result, 10000) == "Error: Exit code: 1\nrejected: copies\n"

    def test_timeout_returns_partial_output(self):
        result = bash(
            "echo first-line; echo err-line >&2; printf no-newline; sleep 10",
            timeout=1,
            user_tier=UserTier.PRIVILEGED,
        )

        assert result.success is False
        assert result.error.startswith("Command timed out after 1 seconds. Output so far:\n")
        assert "first-line" in result.error
        assert "err-line" in result.error
        assert "no-newline" in result.error

    def test_timeout_without_output_keeps_plain_error(self):
        result = bash("sleep 10", timeout=1, user_tier=UserTier.PRIVILEGED)

        assert result.success is False
        assert result.error == "Command timed out after 1 seconds"

    def test_timeout_kills_background_loop(self, tmp_path):
        import time
        marker = tmp_path / "ticks"
        result = bash(
            f"( while true; do echo x >> {marker}; sleep 0.1; done ) & sleep 30",
            timeout=1,
            user_tier=UserTier.PRIVILEGED,
        )
        assert "timed out" in result.error

        size_after_kill = marker.stat().st_size
        assert size_after_kill > 0
        time.sleep(0.6)
        assert marker.stat().st_size == size_after_kill, "background loop survived the timeout"

    def test_timeout_kills_nohup_child(self, tmp_path):
        pid_file = tmp_path / "pid"
        result = bash(
            f"nohup sleep 30 >/dev/null 2>&1 & echo $! > {pid_file}; echo submitted; sleep 30",
            timeout=1,
            user_tier=UserTier.PRIVILEGED,
        )
        assert "submitted" in result.error

        pid = int(pid_file.read_text().strip())
        import time
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.05)
        assert not _pid_alive(pid), "nohup child survived the timeout"

    def test_background_child_holding_pipes_is_reported_and_killed(self, tmp_path):
        import time
        pid_file = tmp_path / "pid"
        result = bash(
            f"echo started; sleep 20 & echo $! > {pid_file}",
            timeout=1,
            user_tier=UserTier.PRIVILEGED,
        )

        assert result.success is False
        assert result.error.startswith(
            "Command exited with code 0, but background processes kept its output open; "
            "they were killed after 1 seconds."
        )
        assert "timed out" not in result.error
        assert "started" in result.error
        pid = int(pid_file.read_text().strip())
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.05)
        assert not _pid_alive(pid), "background child survived"

    def test_on_line_hook_gets_lines_per_stream(self):
        import asyncio
        from src.nixclaw_agent.tools.bash import _OutputCollector

        async def run():
            seen = []
            collector = _OutputCollector(on_line=lambda name, line: seen.append((name, line)))
            process = await asyncio.create_subprocess_shell(
                "echo a; echo b >&2; printf c",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.gather(
                collector.read(process.stdout, "stdout"),
                collector.read(process.stderr, "stderr"),
            )
            await process.wait()
            return seen

        seen = asyncio.run(run())
        assert sorted(seen) == [("stderr", "b\n"), ("stdout", "a\n"), ("stdout", "c")]

    def test_large_output_on_both_pipes_does_not_deadlock(self):
        result = bash(
            "head -c 300000 /dev/zero | tr '\\0' a >&2; head -c 300000 /dev/zero | tr '\\0' b",
            timeout=5,
            user_tier=UserTier.PRIVILEGED,
        )

        assert result.success is True
        assert result.output.count("a") == 300000
        assert result.output.count("b") == 300000


class TestBashRegistryTimeout:
    """The registry's outer timeout for bash must fire after bash's own timeout."""

    @pytest.mark.parametrize("arg, expected", [
        (None, 35.0), (2, 7.0), (300, 305.0), (301, 35.0), (0, 35.0), (-5, 35.0), ("10", 35.0),
        (float("nan"), 35.0), (float("inf"), 35.0), (True, 35.0),
    ])
    @pytest.mark.asyncio
    async def test_bash_outer_timeout_is_bash_timeout_plus_5(self, monkeypatch, arg, expected):
        from src.nixclaw_agent.auth import AuthManager
        from src.nixclaw_agent.config import AuthConfig
        from src.nixclaw_agent.tools import registry as registry_mod
        from src.nixclaw_agent.tools import register_all_tools

        seen = []
        real_wait_for = registry_mod.asyncio.wait_for

        async def recording_wait_for(aw, timeout):
            seen.append(timeout)
            return await real_wait_for(aw, timeout)

        monkeypatch.setattr(registry_mod.asyncio, "wait_for", recording_wait_for)
        registry = registry_mod.ToolRegistry(AuthManager(AuthConfig(privileged_numbers=["+000000"], known_numbers=[])))
        register_all_tools(registry)

        args = {"cmd": "true"}
        if arg is not None:
            args["timeout"] = arg
        await registry.execute_tool("bash", args, UserTier.PRIVILEGED)

        # wait_for is patched process-wide; the bash call's outer timeout is the last one.
        assert seen and seen[-1] == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
