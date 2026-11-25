"""Export and audio analysis tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_export_tools(mcp: FastMCP) -> None:
    """Register export and audio analysis tools with the MCP server."""
    from fl_studio_mcp.utils.audio import (
        bars_to_seconds,
        call_multimodal_api,
        read_bpm_via_gui,
        read_bpm_from_flp,
        slice_wav_single,
        trigger_export_via_gui,
        wav_to_base64,
    )

    @mcp.tool()
    def fl_verify_audio_output(audio_path: str, expected_duration: float | None = None, max_frames: int = 5_000_000) -> dict:
        """Measure a local WAV's duration, peak, RMS, clipping/nonfinite samples and file completeness; no network or project save."""
        from fl_studio_mcp.utils.wav_validation import inspect_wav

        return inspect_wav(audio_path, max_frames, expected_duration)

    @mcp.tool()
    def fl_export_audio(
        output_path: str,
        wait_timeout: float = 120.0,
    ) -> str:
        """Legacy GUI export helper with unverified fixed dialog assumptions.

        This helper is separate from the accepted observed-window export flow.
        It never saves the project or deletes an existing output file. It still
        requires foreground FL and may fail to complete the filename/render
        dialogs. For an observed export, use desktop tools for each dialog and
        fl_verify_audio_output to validate the completed WAV.

        Args:
            output_path: Full path for the exported WAV file.
            wait_timeout: Maximum seconds to wait for render completion.

        Returns:
            Path to the rendered WAV file on success, error message on failure.
        """
        try:
            result_path = trigger_export_via_gui(
                output_path, timeout=wait_timeout
            )
            return str(result_path)
        except Exception as e:
            return f"Export failed: {e}"

    @mcp.tool()
    def fl_read_bpm_from_ui() -> str:
        """Read BPM from FL Studio's tempo display.

        Uses pynput to click the tempo display in FL Studio's transport
        bar, select the text, copy it, and return the BPM value.

        FL Studio must be the active window.

        Returns:
            BPM as a string, or error message on failure.
        """
        try:
            bpm = read_bpm_via_gui()
            return str(bpm)
        except Exception as e:
            return f"Failed to read BPM: {e}"

    @mcp.tool()
    def fl_read_bpm_from_flp(flp_path: str) -> str:
        """Read BPM from an FL Studio project file (.flp).

        Parses the binary project file to extract the tempo
        without needing FL Studio to be running.

        Args:
            flp_path: Path to the .flp project file.

        Returns:
            BPM as a string, or error message on failure.
        """
        try:
            bpm = read_bpm_from_flp(flp_path)
            return str(bpm)
        except Exception as e:
            return f"Failed to read BPM: {e}"

    @mcp.tool()
    def fl_slice_audio(
        audio_path: str,
        start_bar: int,
        end_bar: int,
        output_path: str,
        bpm: float = 120.0,
        beats_per_bar: int = 4,
    ) -> str:
        """Slice a WAV file by bar range.

        Converts bar numbers to seconds using BPM and cuts the WAV.

        Args:
            audio_path: Path to the source WAV file.
            start_bar: Starting bar (1-indexed).
            end_bar: Ending bar (1-indexed, inclusive).
            output_path: Path for the sliced WAV.
            bpm: Tempo in BPM (use fl_get_tempo if unsure).
            beats_per_bar: Beats per bar (4 = 4/4 time).

        Returns:
            Path to the sliced WAV on success, error message on failure.
        """
        try:
            start_sec, end_sec = bars_to_seconds(
                start_bar, end_bar, bpm, beats_per_bar
            )
            result = slice_wav_single(audio_path, start_sec, end_sec, output_path)
            return str(result)
        except Exception as e:
            return f"Slice failed: {e}"

    @mcp.tool()
    def fl_analyze_audio(
        audio_path: str,
        prompt: str = "Describe this audio in detail.",
        api_endpoint: str = "https://api.openai.com/v1/chat/completions",
        api_key: str = "",
        model: str = "gpt-4o-audio-preview",
    ) -> str:
        """Analyze a WAV file with a multimodal AI API.

        Encodes the WAV to base64 and sends it to an OpenAI-compatible
        audio analysis endpoint.

        Args:
            audio_path: Path to the WAV file.
            prompt: What to ask about the audio.
            api_endpoint: API endpoint URL.
            api_key: API key (uses OPENAI_API_KEY env var if empty).
            model: Model name.

        Returns:
            API analysis text.
        """
        try:
            audio_b64 = wav_to_base64(audio_path)
            result = call_multimodal_api(
                audio_base64=audio_b64,
                prompt=prompt,
                api_endpoint=api_endpoint,
                api_key=api_key if api_key else None,
                model=model,
            )
            return str(result)
        except Exception as e:
            return f"Analysis failed: {e}"
