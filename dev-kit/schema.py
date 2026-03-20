"""
dev-kit/schema.py

Pydantic models for every DPG service config.

Each model declares the exact shape of a valid merged config (DPG defaults +
domain values).  Missing required fields or wrong types raise ValidationError
before anything runs.

One top-level model per service:
  AgentCoreConfig
  KnowledgeEngineConfig
  TrustLayerConfig
  MemoryLayerConfig
  LearningLayerConfig
  ActionGatewayConfig
  ReachLayerConfig
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int


class ClientConfig(BaseModel):
    endpoint: str
    timeout_ms: int = 5000


# ---------------------------------------------------------------------------
# Agent Core
# ---------------------------------------------------------------------------

class ConnectorDef(BaseModel):
    name: str
    description: str = ""


class ConnectorsConfig(BaseModel):
    read: list[ConnectorDef] = []
    write: list[ConnectorDef] = []
    identity: list[ConnectorDef] = []


class AgentConfig(BaseModel):
    primary_model: str
    fallback_model: str
    timeout_ms: int = 10000
    retry_attempts: int = 2
    retry_backoff_seconds: list[float] = [0, 0.5, 1.0]
    max_tool_rounds: int = 1


class ConversationAgentConfig(BaseModel):
    max_turns: int = 20
    blocked_message: str = "I'm unable to help with that request."
    escalation_message: str = "I'm connecting you to a human agent who can better assist you."
    output_blocked_message: str = (
        "I wasn't able to produce a safe response. Please try rephrasing your question."
    )


class BhashiniConfig(BaseModel):
    api_key_env: str
    user_id_env: str
    endpoint: str


class LanguageNormalisationConfig(BaseModel):
    model: str
    provider: str = "llm_native"
    supported_languages: list[str]
    transliteration: bool = True
    code_switching: bool = True
    bhashini: BhashiniConfig | None = None


class NLUProcessorConfig(BaseModel):
    model: str
    confidence_threshold: float = 0.5
    history_turns: int = 2
    intents: list[str]
    entities: list[str]
    sentiment_classes: list[str]


class PreprocessingConfig(BaseModel):
    language_normalisation: LanguageNormalisationConfig
    nlu_processor: NLUProcessorConfig


class AgentCoreConfig(BaseModel):
    server: ServerConfig
    agent: AgentConfig
    conversation: ConversationAgentConfig
    connectors: ConnectorsConfig = ConnectorsConfig()
    ke_client: ClientConfig
    memory_client: ClientConfig
    trust_client: ClientConfig
    learning_client: ClientConfig
    action_gateway_client: ClientConfig
    preprocessing: PreprocessingConfig


# ---------------------------------------------------------------------------
# Knowledge Engine
# ---------------------------------------------------------------------------

class GlossaryMapping(BaseModel):
    colloquial: list[str]
    canonical: str


class GlossaryConfig(BaseModel):
    enabled: bool = True
    mappings: list[GlossaryMapping] = []
    apply_to: list[str] = ["normalised_input", "entities"]


class KnowledgeSource(BaseModel):
    path: str
    type: str
    doc_type: str
    refresh: str


class MetadataFiltersConfig(BaseModel):
    use_location_filter: bool = True
    use_intent_filter: bool = True


class StaticKBConfig(BaseModel):
    enabled: bool = True
    vector_store: str = "chromadb"
    collection_name: str
    chroma_persist_dir: str = "./data/chroma_db"
    embedding_provider: str = "sentence_transformers"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    top_k: int = 3
    similarity_threshold: float = 0.65
    sources: list[KnowledgeSource] = []
    metadata_filters: MetadataFiltersConfig = MetadataFiltersConfig()
    intent_filters: dict[str, list[str]] = {}


class MultimodalConfig(BaseModel):
    enabled: bool = False
    supported_types: list[str] = ["pdf", "image"]
    audio_enabled: bool = False
    image_model: str = ""
    max_file_size_mb: int = 10


class KBBlocksConfig(BaseModel):
    glossary: GlossaryConfig = GlossaryConfig()
    static_knowledge_base: StaticKBConfig
    multimodal_input_handler: MultimodalConfig = MultimodalConfig()


class KBInnerConversationConfig(BaseModel):
    max_history_turns: int = 10


class KnowledgeConfig(BaseModel):
    conversation: KBInnerConversationConfig = KBInnerConversationConfig()
    blocks: KBBlocksConfig


class PersonaConfig(BaseModel):
    text: str


class ConversationKEConfig(BaseModel):
    persona: PersonaConfig
    language_instruction: str = ""
    guardrail_reminders: list[str] = []


class KnowledgeEngineConfig(BaseModel):
    server: ServerConfig
    knowledge: KnowledgeConfig
    conversation: ConversationKEConfig


# ---------------------------------------------------------------------------
# Trust Layer
# ---------------------------------------------------------------------------

class InputRulesConfig(BaseModel):
    blocked_phrases: list[str] = []
    escalation_topics: list[str] = []


class OutputRulesConfig(BaseModel):
    blocked_phrases: list[str] = []


class TrustConfig(BaseModel):
    input_rules: InputRulesConfig = InputRulesConfig()
    output_rules: OutputRulesConfig = OutputRulesConfig()


class TrustLayerConfig(BaseModel):
    server: ServerConfig
    trust: TrustConfig


# ---------------------------------------------------------------------------
# Memory Layer
# ---------------------------------------------------------------------------

class MemoryConfig(BaseModel):
    session_ttl_seconds: int = 3600
    max_sessions: int = 1000


class MemoryLayerConfig(BaseModel):
    server: ServerConfig
    memory: MemoryConfig


# ---------------------------------------------------------------------------
# Learning Layer
# ---------------------------------------------------------------------------

class LearningLayerSettings(BaseModel):
    log_level: str = "INFO"


class LearningLayerConfig(BaseModel):
    server: ServerConfig
    learning_layer: LearningLayerSettings


# ---------------------------------------------------------------------------
# Action Gateway
# ---------------------------------------------------------------------------

class ConnectorEndpointConfig(BaseModel):
    endpoint: str
    timeout_ms: int = 5000


class ActionGatewaySettings(BaseModel):
    timeout_ms: int = 5000
    connectors: dict[str, ConnectorEndpointConfig] = {}


class ActionGatewayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=lambda: ServerConfig(port=9999))
    action_gateway: ActionGatewaySettings


# ---------------------------------------------------------------------------
# Reach Layer
# ---------------------------------------------------------------------------

class CLIConfig(BaseModel):
    prompt: str = "You: "
    agent_prefix: str = "Agent: "


class ReachLayerSettings(BaseModel):
    cli: CLIConfig = CLIConfig()


class AgentCoreClientConfig(BaseModel):
    endpoint: str
    timeout_s: float = 30.0


class ReachLayerConfig(BaseModel):
    server: ServerConfig
    reach_layer: ReachLayerSettings
    agent_core_client: AgentCoreClientConfig
