# Windows Installation and First-Run Setup

This early version supports a fixed local memory stack: BAAI/bge-m3 (1024
dimensions) and Qwen/Qwen3-Reranker-0.6B. Other embedding models, remote memory
model APIs, and automatic downloads in Settings are not supported.

Sema does not bundle either model or download model weights during installation:

| Component | Official repository | Upstream license |
| --- | --- | --- |
| Embedding | [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | MIT |
| Reranking | [Qwen/Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) | Apache-2.0 |

Download the models from their official repositories and review the current
model cards and license files before use. Model weights are not covered by
Sema's MIT License. Download size, memory use, startup time, and inference
speed depend on the selected upstream revision and local hardware.

## Prepare the Runtime

Use Windows, Python 3.11 or 3.12, and Node.js 24 LTS. Run commands from the
repository directory. Keep the same activated Python environment when starting
Sema and installing dependencies.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
npm --prefix tui ci
npm --prefix tui run build
.\install-sema.ps1
```

The PATH script only registers the launcher. It does not install dependencies.
Open a new terminal after changing PATH, activate the environment, and run
`sema`. If activation is unavailable, invoke its Python directly:

```powershell
.\.venv\Scripts\python.exe my_agent\main.py
```

SQLite is included in Python; no server, password, or connection URL is needed.
Sema checks FTS5 availability. `usearch` supplies the local vector index.
The tokenizer is `tiktoken` with `cl100k_base`; initial encoding setup may need
internet access. The LLM connection test also verifies the tokenizer can run.

## Configure the LLM

Open Settings > Model > LLM. Enter provider, protocol, endpoint, model and
credentials. Confirm saves changes; Test saved configuration tests the saved
values, not unsaved edits. A successful test is required before first-run chat.
Tests can incur provider charges. Secret input is hidden; leave it empty to
keep the existing key, or use the explicit clear action to remove a saved key.

## Prepare Local Memory Models

Download the complete repositories for `BAAI/bge-m3` and
`Qwen/Qwen3-Reranker-0.6B` using your model download workflow. For example, with
the Hugging Face CLI installed in the active environment:

```powershell
python -m pip install huggingface_hub
hf download BAAI/bge-m3 --local-dir "$HOME\.cache\sema\models\bge-m3"
hf download Qwen/Qwen3-Reranker-0.6B --local-dir "$HOME\.cache\sema\models\qwen3-reranker-0.6b"
py -3.12 -m venv .venv-reranker
.venv-reranker\Scripts\python.exe -m pip install "sentence-transformers>=6,<7"
```

Model download size and time depend on the model repository and network.
Sema's local services run with model downloads disabled. If CUDA is available
to the selected Python environment, they select CUDA; otherwise they use CPU.
CPU startup and inference can be slow. GPU-specific PyTorch installation is
outside this setup flow; use the PyTorch installation instructions for your GPU.

In Settings > Model > Embedding, enter the BGE-M3 directory. It uses Sema's
Python. In Reranker, enter the Qwen directory and the separate environment's
`Scripts/python.exe`. Confirm, then test each saved configuration. A path
existing is not sufficient: tests load the model and validate actual output.
These checks do not create memories or append chat messages.

After changing local-model paths or completing their first tests, use
Preparation > Restart Backend. This disconnects clients for the current
project and is refused while foreground/background work is running. Other
projects' Backends must be restarted separately.

The automated restart test uses a temporary project and user configuration.
It verifies Windows pipe reconnection, not model loading on a second PC.

## Start Without LTM

Once the LLM passes its test, Preparation offers Continue without LTM. This
explicit choice keeps Event Stream, STM and WM working, but disables Memory
Push, long-term retrieval, memorize, Formation and LTM maintenance. The status
line shows LTM unavailable.

During this mode, content that leaves the Formation window is permanently
excluded from automatic LTM formation. Raw chat is retained. Installing the
models later does not backfill excluded content; content still in the window
and new content resume normal processing. A transient failure in a configured
model does not opt into this permanent-skip behavior.

## Optional Services

- Search: enter a Tavily API key. Its test sends a small search request.
- Remote: enter the Telegram Bot Token, allowed User ID and Chat ID. The test
  checks bot credentials and chat access; it does not verify user identity,
  send a message, or start a polling worker. See `remote-control.md` to start
  the Gateway. Restart an existing Gateway after saving its configuration.

## Configuration and Troubleshooting

Settings are stored in the user-level `~/.sema/config.json` (or `SEMA_HOME`).
Saved credentials are plain text in this local file, never displayed as values
by the Settings API. Protect the file and do not commit it. Saved model fields
take precedence over model-related environment variables; environment
variables are used when the corresponding field has no saved value. Status
reports an environment source only when it is the effective source.

Model-related overrides remain supported as fallbacks. Other service-specific
environment variables remain effective overrides: `SEMA_EMBEDDING_MODEL_PATH`,
`SEMA_RERANKER_MODEL_PATH`, `SEMA_RERANKER_PYTHON`, `TAVILY_API_KEY`, and
`SEMA_TELEGRAM_BOT_TOKEN` / `SEMA_TELEGRAM_USER_ID` / `SEMA_TELEGRAM_CHAT_ID`.
After saving a field in Settings, restart Sema if the running Backend was
started with an older effective configuration.

Missing means a required value/path is absent. Not tested means the current
configuration has no valid recorded test. Failed means the test did not pass.
Restart required means saved local configuration differs from the Backend's
startup configuration. Run tests again after changing model files or paths.

## Second-PC Acceptance

On a separate Windows PC, follow the installation steps without copying a
developer `.env`. Configure/test the LLM and explicitly continue without LTM.
Verify chat and STM/WM operation. Install/configure/test both local models,
restart Backend, and verify a new memory can be formed and retrieved. Confirm
that previously skipped history is not automatically backfilled. Optional
Telegram and search must not be required for ordinary local chat.
