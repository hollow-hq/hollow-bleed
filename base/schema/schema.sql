CREATE TABLE IF NOT EXISTS lastfm (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS prefix (
    guild_id BIGINT PRIMARY KEY,
    prefix VARCHAR(7)
);

CREATE TABLE IF NOT EXISTS lost_boosters (
    guild_id BIGINT,
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    discriminator TEXT,
    lost_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS antiraid (
    guild_id BIGINT,
    command TEXT,
    punishment TEXT,
    seconds INTEGER
);

CREATE TABLE IF NOT EXISTS whitelist (
    guild_id BIGINT,
    module TEXT,
    object_id BIGINT,
    mode TEXT
);

CREATE TABLE IF NOT EXISTS guild_whitelist (
    guild_id BIGINT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS blacklist (
    user_id BIGINT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS guild_blacklist (
    guild_id BIGINT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS afk (
    user_id BIGINT PRIMARY KEY,
    status TEXT,
    time BIGINT
);

CREATE TABLE IF NOT EXISTS forcenick (
    guild_id BIGINT,
    user_id BIGINT,
    name TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS lockdown (
    guild_id BIGINT,
    channel_id BIGINT,
    role_id BIGINT
);

CREATE TABLE IF NOT EXISTS welcome (
    guild_id BIGINT,
    channel_id BIGINT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS boost (
    guild_id BIGINT,
    channel_id BIGINT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS warnings (
    guild_id BIGINT,
    user_id BIGINT,
    warns INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS aliases (
    guild_id BIGINT,
    alias TEXT,
    command TEXT,
    invoke TEXT,
    PRIMARY KEY (guild_id, alias)
);

CREATE TABLE IF NOT EXISTS autorole (
    guild_id BIGINT,
    role_id BIGINT,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS sticky_messages (
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    message TEXT,
    schedule TEXT,
    PRIMARY KEY (guild_id, channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS autoresponders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    `trigger` TEXT NOT NULL,
    response TEXT NOT NULL,
    not_strict BOOLEAN DEFAULT FALSE,
    self_destruct BOOLEAN DEFAULT FALSE,
    self_destruct_time INT DEFAULT 0,
    delete_trigger BOOLEAN DEFAULT FALSE,
    reply BOOLEAN DEFAULT FALSE,
    UNIQUE(guild_id, `trigger`)
);

CREATE TABLE IF NOT EXISTS booster_module (
    guild_id BIGINT PRIMARY KEY,
    base BIGINT DEFAULT NULL
);


CREATE TABLE IF NOT EXISTS booster_roles (
    guild_id BIGINT,
    user_id BIGINT,
    role_id BIGINT,
    PRIMARY KEY (guild_id, user_id)
);


CREATE TABLE IF NOT EXISTS br_award (
    guild_id BIGINT,
    role_id BIGINT,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS `leave` (
    guild_id BIGINT,
    channel_id BIGINT,
    message TEXT,
    PRIMARY KEY(guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS fake_permissions (
    guild_id bigint NOT NULL,
    role_id bigint NOT NULL,
    permission text NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS birthdays (
    user_id BIGINT PRIMARY KEY,
    month TINYINT NOT NULL,
    day TINYINT NOT NULL,
    year SMALLINT NULL,
    locked TINYINT(1) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS birthday_config (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NULL,
    role_id BIGINT NULL
);

CREATE TABLE IF NOT EXISTS birthday_roles (
    guild_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS birthday_log (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    year SMALLINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS diary (
    user_id BIGINT NOT NULL,
    entry_id INT NOT NULL,
    content TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    is_private TINYINT(1) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, entry_id)
);

CREATE TABLE IF NOT EXISTS chatbot_config (
    guild_id BIGINT PRIMARY KEY,
    enabled TINYINT(1) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chatbot_channels (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS self_prefixes (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    prefix VARCHAR(7) NOT NULL,
    PRIMARY KEY (guild_id, user_id, prefix)
);

CREATE TABLE IF NOT EXISTS noprefix (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- =========================================================================
-- Tickets module
-- =========================================================================

CREATE TABLE IF NOT EXISTS ticket_panels (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name VARCHAR(120) NOT NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'button',
    channel_id BIGINT NULL,
    message_id BIGINT NULL,
    log_channel_id BIGINT NULL,
    default_category_id BIGINT NULL,
    overflow_category_id BIGINT NULL,
    delete_delay INT NOT NULL DEFAULT 0,
    max_open_per_member INT NOT NULL DEFAULT 1,
    auto_pin TINYINT(1) NOT NULL DEFAULT 0,
    claims_enabled TINYINT(1) NOT NULL DEFAULT 1,
    name_format VARCHAR(255) NULL,
    case_padding INT NOT NULL DEFAULT 3,
    dropdown_placeholder VARCHAR(255) NULL,
    panel_message LONGTEXT NULL,
    log_message LONGTEXT NULL,
    auto_delete_message LONGTEXT NULL,
    next_case_id INT NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL,
    UNIQUE KEY uniq_panel_name (guild_id, name)
);

CREATE TABLE IF NOT EXISTS ticket_options (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    panel_id BIGINT NOT NULL,
    position INT NOT NULL DEFAULT 0,
    active TINYINT(1) NOT NULL DEFAULT 1,
    label VARCHAR(120) NOT NULL,
    emoji VARCHAR(120) NULL,
    description VARCHAR(255) NULL,
    style VARCHAR(16) NOT NULL DEFAULT 'secondary',
    form_id BIGINT NULL,
    category_id BIGINT NULL,
    claim_category_id BIGINT NULL,
    close_category_id BIGINT NULL,
    name_format VARCHAR(255) NULL,
    claim_name_format VARCHAR(255) NULL,
    close_name_format VARCHAR(255) NULL,
    member_required_roles TEXT NULL,
    member_match_all TINYINT(1) NOT NULL DEFAULT 0,
    creator_can_close TINYINT(1) NOT NULL DEFAULT 1,
    close_on_leave TINYINT(1) NOT NULL DEFAULT 0,
    support_role_ids TEXT NULL,
    keep_staff_visible TINYINT(1) NOT NULL DEFAULT 0,
    staff_can_speak TINYINT(1) NOT NULL DEFAULT 0,
    trainee_role_ids TEXT NULL,
    trainee_can_claim TINYINT(1) NOT NULL DEFAULT 0,
    trainee_can_close TINYINT(1) NOT NULL DEFAULT 0,
    trainee_can_speak TINYINT(1) NOT NULL DEFAULT 0,
    button_claim LONGTEXT NULL,
    button_close LONGTEXT NULL,
    button_reopen LONGTEXT NULL,
    button_delete LONGTEXT NULL,
    greeting_message LONGTEXT NULL,
    greeting_dm LONGTEXT NULL,
    claim_message LONGTEXT NULL,
    move_message LONGTEXT NULL,
    close_message LONGTEXT NULL,
    close_dm LONGTEXT NULL,
    reopen_message LONGTEXT NULL,
    reopen_dm LONGTEXT NULL,
    auto_close_message LONGTEXT NULL,
    auto_close_after INT NULL,
    auto_delete_message LONGTEXT NULL,
    auto_delete_after INT NULL,
    inactivity_message LONGTEXT NULL,
    inactivity_time INT NULL,
    required_roles_message LONGTEXT NULL,
    created_at BIGINT NOT NULL,
    KEY idx_option_panel (panel_id),
    KEY idx_option_guild (guild_id)
);

CREATE TABLE IF NOT EXISTS ticket_forms (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name VARCHAR(120) NOT NULL,
    title VARCHAR(120) NULL,
    filtering TINYINT(1) NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL,
    UNIQUE KEY uniq_form_name (guild_id, name)
);

CREATE TABLE IF NOT EXISTS ticket_form_fields (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    form_id BIGINT NOT NULL,
    position INT NOT NULL DEFAULT 0,
    label VARCHAR(120) NOT NULL,
    field_key VARCHAR(120) NOT NULL,
    field_type VARCHAR(32) NOT NULL,
    required TINYINT(1) NOT NULL DEFAULT 1,
    description VARCHAR(255) NULL,
    placeholder VARCHAR(255) NULL,
    min_length INT NULL,
    max_length INT NULL,
    checked_text VARCHAR(120) NULL,
    unchecked_text VARCHAR(120) NULL,
    min_values INT NULL,
    max_values INT NULL,
    choices TEXT NULL,
    KEY idx_field_form (form_id)
);

CREATE TABLE IF NOT EXISTS ticket_cases (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    case_number INT NOT NULL,
    case_id VARCHAR(64) NOT NULL,
    ticket_id BIGINT NULL,
    created_at BIGINT NOT NULL,
    UNIQUE KEY uniq_case (guild_id, case_id),
    KEY idx_case_number (guild_id, case_number)
);

CREATE TABLE IF NOT EXISTS tickets (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    case_id VARCHAR(64) NOT NULL,
    case_number INT NOT NULL,
    panel_id BIGINT NOT NULL,
    option_id BIGINT NOT NULL,
    channel_id BIGINT NULL,
    author_id BIGINT NOT NULL,
    claimer_id BIGINT NULL,
    closer_id BIGINT NULL,
    reopener_id BIGINT NULL,
    deleter_id BIGINT NULL,
    opened_label VARCHAR(120) NULL,
    opened_at BIGINT NOT NULL,
    claimed_at BIGINT NULL,
    migrated_at BIGINT NULL,
    closed_at BIGINT NULL,
    reopened_at BIGINT NULL,
    deleted_at BIGINT NULL,
    last_author_message_at BIGINT NULL,
    last_inactivity_notice_at BIGINT NULL,
    auto_delete_at BIGINT NULL,
    inactivity_notice_due BIGINT NULL,
    auto_close_due BIGINT NULL,
    closed_automatically TINYINT(1) NOT NULL DEFAULT 0,
    deleted TINYINT(1) NOT NULL DEFAULT 0,
    is_open TINYINT(1) NOT NULL DEFAULT 1,
    claim_reason TEXT NULL,
    migrate_reason TEXT NULL,
    close_reason TEXT NULL,
    reopen_reason TEXT NULL,
    delete_reason TEXT NULL,
    form_id BIGINT NULL,
    form_submitted_at BIGINT NULL,
    form_answers LONGTEXT NULL,
    allowed_ids TEXT NULL,
    trainee_overrides TEXT NULL,
    KEY idx_ticket_guild (guild_id),
    KEY idx_ticket_channel (channel_id),
    KEY idx_ticket_case (guild_id, case_id),
    KEY idx_ticket_open (guild_id, is_open)
);

CREATE TABLE IF NOT EXISTS ticket_transcripts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    case_id VARCHAR(64) NOT NULL,
    ticket_id BIGINT NULL,
    channel_id BIGINT NULL,
    url TEXT NULL,
    content LONGTEXT NULL,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NULL,
    UNIQUE KEY uniq_transcript (guild_id, case_id)
);

CREATE TABLE IF NOT EXISTS ticket_profiles (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    claim_category_id BIGINT NULL,
    claim_name_format VARCHAR(255) NULL,
    claim_message LONGTEXT NULL,
    UNIQUE KEY uniq_profile (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS ticket_config (
    guild_id BIGINT PRIMARY KEY,
    staff_role_ids TEXT NULL,
    profiles_enabled TINYINT(1) NOT NULL DEFAULT 1,
    blacklist_members TEXT NULL,
    blacklist_roles TEXT NULL
);

CREATE TABLE IF NOT EXISTS voicemaster (
    guild_id BIGINT PRIMARY KEY,
    category_id BIGINT,
    interface_id BIGINT,
    create_channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS voicemaster_channels (
    guild_id BIGINT,
    channel_id BIGINT PRIMARY KEY,
    owner_id BIGINT,
    locked TINYINT DEFAULT 0,
    hidden TINYINT DEFAULT 0,
    created_at BIGINT
);

CREATE TABLE IF NOT EXISTS voicemaster_interfaces (
    guild_id BIGINT,
    channel_id BIGINT PRIMARY KEY,
    message_id BIGINT
);


CREATE TABLE IF NOT EXISTS server_counters (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT PRIMARY KEY,
    counter_type VARCHAR(32) NOT NULL,
    base_name VARCHAR(100) NOT NULL
);

-- =========================================================================
-- Giveaways (engagement cog)
-- =========================================================================

-- One row per giveaway. Draft rows are setup panels that have not been
-- started yet, running rows are live giveaways and ended rows are history.
CREATE TABLE IF NOT EXISTS giveaways (
    id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NULL,
    host_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'draft',
    prize VARCHAR(256) NULL,
    duration BIGINT NULL,
    ends_at BIGINT NULL,
    required_role_id BIGINT NULL,
    required_age BIGINT NULL,
    required_level INT NULL,
    max_entries INT NULL,
    winners INT NOT NULL DEFAULT 1,
    embed_script LONGTEXT NULL,
    panel_message_id BIGINT NULL,
    message_id BIGINT NULL,
    winner_ids TEXT NULL,
    reroll_count INT NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL,
    started_at BIGINT NULL,
    ended_at BIGINT NULL,
    UNIQUE KEY uniq_giveaway_message (guild_id, message_id),
    KEY idx_giveaway_guild (guild_id),
    KEY idx_giveaway_due (status, ends_at)
);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    entered_at BIGINT NOT NULL,
    PRIMARY KEY (giveaway_id, user_id),
    KEY idx_entry_user (user_id)
);

-- Roles/users that may never enter a giveaway in the guild.
CREATE TABLE IF NOT EXISTS giveaway_blacklist (
    guild_id BIGINT NOT NULL,
    object_id BIGINT NOT NULL,
    mode VARCHAR(8) NOT NULL,
    PRIMARY KEY (guild_id, object_id, mode)
);

-- Default channel where new giveaways are posted when one is configured.
CREATE TABLE IF NOT EXISTS giveaway_config (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NULL
);

-- =========================================================================
-- Leveling (level cog)
-- =========================================================================

-- One row per (guild, member) holding the total XP that member has earned.
-- Level and rank are always derived from `xp` by a shared helper, so they are
-- deliberately not stored.
CREATE TABLE IF NOT EXISTS levels (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    xp BIGINT NOT NULL DEFAULT 0,
    messages BIGINT NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id),
    KEY idx_levels_rank (guild_id, xp)
);

-- Per-guild leveling configuration: master switch, XP range, level-up
-- channel/message, and the customisable embed colours.
CREATE TABLE IF NOT EXISTS level_config (
    guild_id BIGINT PRIMARY KEY,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    min_xp INT NOT NULL DEFAULT 15,
    max_xp INT NOT NULL DEFAULT 25,
    xp_cooldown INT NOT NULL DEFAULT 60,
    announce_levelup TINYINT(1) NOT NULL DEFAULT 1,
    levelup_channel_id BIGINT NULL,
    levelup_message VARCHAR(512) NULL,
    color_neutral INT NULL,
    color_approve INT NULL,
    color_warn INT NULL,
    color_deny INT NULL
);

-- Channels excluded from XP gain. `mode` is 'channel' for text channels and
-- 'role' for roles, so one table serves `,level exempt` for both.
CREATE TABLE IF NOT EXISTS level_exempt (
    guild_id BIGINT NOT NULL,
    object_id BIGINT NOT NULL,
    mode VARCHAR(8) NOT NULL,
    PRIMARY KEY (guild_id, object_id, mode)
);

-- =========================================================================
-- Highlights (engagement cog)
-- =========================================================================

-- One row per highlighted keyword. Keywords are per-user, per-guild and are
-- stored lowercased; the listener matches them as whole words/phrases. The
-- primary key enforces the unique (guild_id, user_id, keyword) constraint.
CREATE TABLE IF NOT EXISTS highlight_keywords (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    keyword VARCHAR(100) NOT NULL,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id, keyword),
    KEY idx_highlight_guild (guild_id)
);

-- Notification ignores of `,highlight ignore`. `mode` is 'user' for members,
-- 'channel' for text channels and 'role' for roles, so one table serves all
-- three subcommands (same convention as giveaway_blacklist / level_exempt).
CREATE TABLE IF NOT EXISTS highlight_ignores (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    object_id BIGINT NOT NULL,
    mode VARCHAR(8) NOT NULL,
    PRIMARY KEY (guild_id, user_id, object_id, mode),
    KEY idx_highlight_ignore_user (guild_id, user_id)
);

-- =========================================================================
-- Idempotent migrations
-- =========================================================================
-- ``CREATE TABLE IF NOT EXISTS`` above only creates missing tables, so columns
-- added to an already-existing table afterwards are applied here. These run on
-- every boot; the loader tolerates the duplicate-column error MySQL raises when
-- the column already exists, which keeps them idempotent.

-- migrate giveaways: minimum member level required to enter a giveaway.
ALTER TABLE giveaways ADD COLUMN required_level INT NULL;