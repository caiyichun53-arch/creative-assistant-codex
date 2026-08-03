-- Formal external-publication registration and P0-P7 feedback chain.
-- This stores only user-confirmed external facts and observations. It never
-- creates, publishes, sends, or automatically changes long-term rules.
CREATE TABLE IF NOT EXISTS stage0_publication_registration (
    publication_id TEXT PRIMARY KEY,
    content_account_id TEXT NOT NULL,
    domain_label TEXT NOT NULL,
    task_id TEXT NOT NULL,
    audio_delivery_id TEXT NOT NULL,
    approved_content_version_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    external_video_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    actual_content_status TEXT NOT NULL CHECK(actual_content_status IN ('same_as_approved', 'different_from_approved')),
    actual_content_note TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered' CHECK(status IN ('registered', 'closed')),
    data_identity TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform, external_video_url, data_identity)
);

CREATE INDEX IF NOT EXISTS idx_stage0_publication_registration_domain
ON stage0_publication_registration(domain_label, content_account_id, data_identity);

CREATE TABLE IF NOT EXISTS stage0_publication_observation (
    observation_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES stage0_publication_registration(publication_id) ON DELETE RESTRICT,
    point_code TEXT NOT NULL CHECK(point_code IN ('P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7')),
    observation_status TEXT NOT NULL CHECK(observation_status IN ('recorded', 'missing')),
    metrics_json TEXT NOT NULL DEFAULT '{}',
    missing_reason TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    data_identity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(publication_id, point_code, data_identity)
);

CREATE INDEX IF NOT EXISTS idx_stage0_publication_observation_publication
ON stage0_publication_observation(publication_id, point_code, data_identity);

CREATE TABLE IF NOT EXISTS stage0_publication_p7_review (
    review_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES stage0_publication_registration(publication_id) ON DELETE RESTRICT,
    selection_assessment TEXT NOT NULL,
    narrative_assessment TEXT NOT NULL,
    material_assessment TEXT NOT NULL,
    external_conditions_assessment TEXT NOT NULL,
    feedback_candidate_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('awaiting_user_confirmation', 'confirmed', 'rejected')),
    decision_by TEXT,
    decision_reason TEXT,
    decided_at TEXT,
    data_identity TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(publication_id, data_identity)
);

CREATE INDEX IF NOT EXISTS idx_stage0_publication_p7_review_publication
ON stage0_publication_p7_review(publication_id, data_identity);
