CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);
CREATE TABLE public.game_configs (
    id bigint NOT NULL,
    game_id bigint NOT NULL,
    config_schema_version integer NOT NULL,
    config json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.game_configs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.game_configs_id_seq OWNED BY public.game_configs.id;
CREATE TABLE public.games (
    id bigint NOT NULL,
    public_id character(32) NOT NULL,
    room_code character(6) NOT NULL,
    host_user_id bigint,
    host_display_name character varying(24) NOT NULL,
    rng_seed bigint NOT NULL,
    duration_days integer NOT NULL,
    days_played integer NOT NULL,
    ended_early boolean NOT NULL,
    player_count integer NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.games_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.games_id_seq OWNED BY public.games.id;
CREATE TABLE public.market_days (
    id bigint NOT NULL,
    game_id bigint NOT NULL,
    day integer NOT NULL,
    weekday character varying(3) NOT NULL,
    weekday_multiplier double precision NOT NULL,
    expected_demand double precision NOT NULL,
    demand_total integer NOT NULL,
    capacity integer NOT NULL,
    disrupted boolean NOT NULL,
    spot_price numeric(12,2) NOT NULL,
    cooperation boolean NOT NULL,
    cooperators integer NOT NULL,
    refrigeration_failure boolean NOT NULL,
    units_sold_total integer NOT NULL,
    peer_units integer NOT NULL,
    peer_avg_price numeric(12,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.market_days_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.market_days_id_seq OWNED BY public.market_days.id;
CREATE TABLE public.participants (
    id bigint NOT NULL,
    game_id bigint NOT NULL,
    alias character varying(4) NOT NULL,
    display_name character varying(24) NOT NULL,
    user_id bigint,
    guest_identity character varying(64),
    final_rank integer NOT NULL,
    total_profit numeric(12,2) NOT NULL,
    units_sold integer NOT NULL,
    units_spoiled integer NOT NULL,
    units_lost_transport integer NOT NULL,
    overall_market_share double precision,
    fill_rate double precision,
    waste_rate double precision,
    avg_reputation double precision NOT NULL,
    final_reputation double precision NOT NULL,
    futures_units integer NOT NULL,
    cost_insurance numeric(12,2) NOT NULL,
    refund_insurance numeric(12,2) NOT NULL,
    peer_units_bought integer NOT NULL,
    peer_units_sold integer NOT NULL,
    cost_peer numeric(12,2) NOT NULL,
    revenue_peer numeric(12,2) NOT NULL,
    liquidated_units integer NOT NULL,
    revenue_liquidation numeric(12,2) NOT NULL,
    refund_insurance_in_transit numeric(12,2) NOT NULL,
    futures_cancelled_units integer NOT NULL,
    refund_futures_cancelled numeric(12,2) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.participants_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.participants_id_seq OWNED BY public.participants.id;
CREATE TABLE public.player_days (
    id bigint NOT NULL,
    game_id bigint NOT NULL,
    day integer NOT NULL,
    alias character varying(4) NOT NULL,
    weekday character varying(3) NOT NULL,
    order_spot integer NOT NULL,
    order_futures integer NOT NULL,
    incoterm character varying(3) NOT NULL,
    conserve_lots json NOT NULL,
    lots_conserved integer NOT NULL,
    cooperate boolean NOT NULL,
    peer_buy json,
    peer_sell json,
    outlay numeric(12,2) NOT NULL,
    opening_stock integer NOT NULL,
    futures_due integer NOT NULL,
    futures_filled integer NOT NULL,
    futures_unfilled integer NOT NULL,
    allocated_spot integer NOT NULL,
    shortfall_spot integer NOT NULL,
    shipped integer NOT NULL,
    lost_transport integer NOT NULL,
    delivered integer NOT NULL,
    peer_bought integer NOT NULL,
    peer_sold integer NOT NULL,
    peer_trades json NOT NULL,
    demand integer NOT NULL,
    backlog_open integer NOT NULL,
    sold integer NOT NULL,
    backlog_end integer NOT NULL,
    spoiled integer NOT NULL,
    closing_stock integer NOT NULL,
    revenue_sales numeric(12,2) NOT NULL,
    revenue_salvage numeric(12,2) NOT NULL,
    revenue_peer numeric(12,2) NOT NULL,
    refund_insurance numeric(12,2) NOT NULL,
    cost_spot numeric(12,2) NOT NULL,
    cost_futures numeric(12,2) NOT NULL,
    cost_peer numeric(12,2) NOT NULL,
    refund_futures numeric(12,2) NOT NULL,
    cost_transport numeric(12,2) NOT NULL,
    cost_insurance numeric(12,2) NOT NULL,
    cost_conservation numeric(12,2) NOT NULL,
    cost_holding numeric(12,2) NOT NULL,
    cost_backlog numeric(12,2) NOT NULL,
    profit numeric(12,2) NOT NULL,
    cash_open numeric(12,2) NOT NULL,
    cash_end numeric(12,2) NOT NULL,
    futures_vs_spot numeric(12,2) NOT NULL,
    fill double precision,
    waste double precision,
    reputation_open double precision NOT NULL,
    reputation_end double precision NOT NULL,
    market_share double precision,
    refrigeration_failure boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.player_days_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.player_days_id_seq OWNED BY public.player_days.id;
CREATE TABLE public.user_stats (
    user_id bigint NOT NULL,
    games_played integer NOT NULL,
    wins integer NOT NULL,
    total_profit numeric(12,2) NOT NULL,
    best_profit numeric(12,2) NOT NULL,
    avg_fill_rate double precision,
    avg_waste_rate double precision,
    avg_market_share double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.users (
    id bigint NOT NULL,
    firebase_uid character varying(128) NOT NULL,
    display_name character varying(24) NOT NULL,
    email character varying(320),
    photo_url character varying(1024),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;
ALTER TABLE ONLY public.game_configs ALTER COLUMN id SET DEFAULT nextval('public.game_configs_id_seq'::regclass);
ALTER TABLE ONLY public.games ALTER COLUMN id SET DEFAULT nextval('public.games_id_seq'::regclass);
ALTER TABLE ONLY public.market_days ALTER COLUMN id SET DEFAULT nextval('public.market_days_id_seq'::regclass);
ALTER TABLE ONLY public.participants ALTER COLUMN id SET DEFAULT nextval('public.participants_id_seq'::regclass);
ALTER TABLE ONLY public.player_days ALTER COLUMN id SET DEFAULT nextval('public.player_days_id_seq'::regclass);
ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);
ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT pk_game_configs PRIMARY KEY (id);
ALTER TABLE ONLY public.games
    ADD CONSTRAINT pk_games PRIMARY KEY (id);
ALTER TABLE ONLY public.market_days
    ADD CONSTRAINT pk_market_days PRIMARY KEY (id);
ALTER TABLE ONLY public.participants
    ADD CONSTRAINT pk_participants PRIMARY KEY (id);
ALTER TABLE ONLY public.player_days
    ADD CONSTRAINT pk_player_days PRIMARY KEY (id);
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT pk_user_stats PRIMARY KEY (user_id);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT uq_game_configs_game_id UNIQUE (game_id);
ALTER TABLE ONLY public.games
    ADD CONSTRAINT uq_games_public_id UNIQUE (public_id);
ALTER TABLE ONLY public.market_days
    ADD CONSTRAINT uq_market_days_game_id_day UNIQUE (game_id, day);
ALTER TABLE ONLY public.participants
    ADD CONSTRAINT uq_participants_game_id_alias UNIQUE (game_id, alias);
ALTER TABLE ONLY public.player_days
    ADD CONSTRAINT uq_player_days_game_id_day_alias UNIQUE (game_id, day, alias);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid);
CREATE INDEX ix_games_room_code ON public.games USING btree (room_code);
CREATE INDEX ix_participants_guest_identity ON public.participants USING btree (guest_identity);
CREATE INDEX ix_participants_user_id ON public.participants USING btree (user_id);
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT fk_game_configs_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.games
    ADD CONSTRAINT fk_games_host_user_id_users FOREIGN KEY (host_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.market_days
    ADD CONSTRAINT fk_market_days_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.participants
    ADD CONSTRAINT fk_participants_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.participants
    ADD CONSTRAINT fk_participants_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.player_days
    ADD CONSTRAINT fk_player_days_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT fk_user_stats_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
