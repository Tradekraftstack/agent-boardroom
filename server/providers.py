"""Model providers a room can use as always-on members (server-side) or as runners (local).

Every one of these speaks the OpenAI-style /chat/completions API, so adding a provider
is one entry here. "where" tells the owner who receives what the model is sent.
"model" is only a starting point: model names change often, so the
app's "Load models" button asks the provider for its live list.
"""

PROVIDERS = {
    "openrouter": {"where": "OpenRouter (USA), passed on to whichever model's company you pick", "label": "OpenRouter", "name": "OPENROUTER", "base": "https://openrouter.ai/api/v1",
                   "model": "openrouter/auto", "key": True,
                   "hint": "One key for hundreds of models (Kimi, DeepSeek, Qwen, Grok, Llama...). Key: openrouter.ai/keys"},
    "nvidia": {"where": "NVIDIA (USA)", "label": "NVIDIA (build.nvidia.com)", "name": "NVIDIA", "base": "https://integrate.api.nvidia.com/v1",
               "model": "nvidia/nemotron-3-super-120b-a12b", "key": True,
               "hint": "NVIDIA-hosted Nemotron, Llama, DeepSeek, Kimi, Qwen. Key: build.nvidia.com"},
    "kimi": {"where": "Moonshot AI (China)", "label": "Kimi (Moonshot AI)", "name": "KIMI", "base": "https://api.moonshot.ai/v1",
             "model": "kimi-k3", "key": True, "hint": "Key: platform.moonshot.ai"},
    "deepseek": {"where": "DeepSeek (China)", "label": "DeepSeek", "name": "DEEPSEEK", "base": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "key": True, "hint": "Key: platform.deepseek.com"},
    "qwen": {"where": "Alibaba Cloud (Chinese company; international servers in Singapore)", "label": "Qwen (Alibaba Cloud Model Studio)", "name": "QWEN",
             "base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "key": True,
             "hint": "Key: Alibaba Cloud Model Studio (DashScope). Mainland China accounts: dashscope.aliyuncs.com"},
    "grok": {"where": "xAI (USA)", "label": "Grok (xAI)", "name": "GROK", "base": "https://api.x.ai/v1", "model": "grok-4.7", "key": True,
             "hint": "Key: console.x.ai"},
    "openai": {"where": "OpenAI (USA)", "label": "OpenAI (ChatGPT models)", "name": "GPT", "base": "https://api.openai.com/v1",
               "model": "gpt-6.1-sol", "key": True, "hint": "Key: platform.openai.com"},
    "anthropic": {"where": "Anthropic (USA)", "label": "Anthropic (Claude models)", "name": "CLAUDEAPI", "base": "https://api.anthropic.com/v1",
                  "model": "claude-sonnet-5-5", "key": True, "hint": "Key: console.anthropic.com"},
    "gemini": {"where": "Google (USA)", "label": "Google Gemini", "name": "GEMINI",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-3.8-flash",
               "key": True, "hint": "Key: aistudio.google.com"},
    "mistral": {"where": "Mistral AI (France, EU)", "label": "Mistral", "name": "MISTRAL", "base": "https://api.mistral.ai/v1",
                "model": "mistral-large-latest", "key": True, "hint": "Key: console.mistral.ai"},
    "groq": {"where": "Groq (USA)", "label": "Groq (fast open models)", "name": "GROQ", "base": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile", "key": True, "hint": "Key: console.groq.com"},
    "agnes": {"where": "Agnes AI (check their privacy policy)", "label": "Agnes AI", "name": "AGNES", "base": "https://apihub.agnes-ai.com/v1",
              "model": "agnes-2.0-flash", "key": True, "hint": "Key: from Agnes AI"},
    "ollama": {"where": "Nowhere. Stays on the computer running it", "label": "Ollama (local)", "name": "OLLAMA", "base": "http://localhost:11434/v1", "model": "llama3.2",
               "key": False, "local": True,
               "hint": "Free, runs on your machine. If the room runs in Docker, use http://host.docker.internal:11434/v1"},
    "lmstudio": {"where": "Nowhere. Stays on the computer running it", "label": "LM Studio (local)", "name": "LMSTUDIO", "base": "http://localhost:1234/v1", "model": "",
                 "key": False, "local": True,
                 "hint": "Start LM Studio's server first. In Docker, use http://host.docker.internal:1234/v1"},
    "custom": {"where": "Wherever that server is", "label": "Other (any OpenAI-compatible API)", "name": "MODEL", "base": "", "model": "", "key": False,
               "hint": "vLLM, llama.cpp, Together, Fireworks, a company gateway... paste its base URL (ending in /v1)"},
}


def resolve(member):
    """(base, model) for a server member, falling back to its provider's defaults."""
    p = PROVIDERS.get(member.get("provider", "agnes"), PROVIDERS["custom"])
    return (member.get("base") or p["base"]).rstrip("/"), member.get("model") or p["model"]
