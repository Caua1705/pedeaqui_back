--
-- Schema completo do banco, na revisao 20260810_0012 (head).
--
-- POR QUE ESTE ARQUIVO EXISTE
--
-- A revisao de baseline do Alembic (20260726_0001) e um NO-OP: o schema de
-- producao nasceu antes do Alembic, construido a mao no Supabase e remendado
-- pelos 12 .sql de migrations/. Nenhum dos dois constroi um banco do zero, e
-- por isso `alembic upgrade head` num banco VAZIO morre na revisao 0002 com
-- "relation orders does not exist".
--
-- Este arquivo fecha esse buraco. E um `pg_dump --schema-only` do banco de
-- producao (PG 17.6), com tudo que o ORM nao mapeia e que a armadilha 24
-- avisa que se perderia num Base.metadata.create_all(): as sequences
-- (inclusive orders_order_number_seq), os DEFAULT, os CHECK e os indices
-- criados a mao.
--
-- COMO USAR (e o que a fixture da suite `db` faz):
--
--     psql "$URL" -f alembic/schema_baseline.sql
--     alembic stamp 20260810_0012
--     alembic upgrade head        # aplica so o que vier depois
--
-- QUANDO REGERAR: nunca por rotina. Toda mudanca de schema daqui em diante e
-- uma revisao do Alembic, que este arquivo NAO precisa conhecer — o `stamp`
-- fixa a foto em 0012 e o `upgrade` aplica o resto por cima. Regerar so faz
-- sentido se alguem mexer no banco por fora do Alembic de novo, que e
-- exatamente o que nao deve voltar a acontecer.
--
-- Editado apos o pg_dump: removidos os meta-comandos \restrict e \unrestrict
-- (o pg_dump 17.10 os emite e eles so existem no psql) e o
-- `CREATE SCHEMA public`, que ja existe em banco novo.
--

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: rls_auto_enable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rls_auto_enable() RETURNS event_trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$$;


--
-- Name: update_updated_at_column(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_updated_at_column() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
  new.updated_at = now();
  return new;
end;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: admin_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    branch_id uuid,
    name text NOT NULL,
    email text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT admin_users_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'manager'::text, 'attendant'::text]))),
    CONSTRAINT ck_admin_users_role CHECK ((role = ANY (ARRAY['owner'::text, 'manager'::text, 'attendant'::text])))
);


--
-- Name: ai_feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_feedback (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    session_id text NOT NULL,
    user_message text,
    assistant_message text,
    response_type text NOT NULL,
    selected_product_ids uuid[] DEFAULT '{}'::uuid[],
    feedback text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ai_feedback_feedback_check CHECK ((feedback = ANY (ARRAY['like'::text, 'dislike'::text])))
);


--
-- Name: ai_product_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_product_embeddings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    product_id uuid,
    content text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    embedding public.vector(1536),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    content_hash text
);


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: branch_business_hours; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.branch_business_hours (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    branch_id uuid NOT NULL,
    weekday integer NOT NULL,
    opens_at time without time zone,
    closes_at time without time zone,
    is_closed boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    prep_time_min integer,
    prep_time_max integer,
    CONSTRAINT branch_business_hours_prep_time_valid CHECK (((prep_time_min IS NULL) OR (prep_time_max IS NULL) OR ((prep_time_min >= 0) AND (prep_time_max >= prep_time_min)))),
    CONSTRAINT branch_business_hours_valid_time CHECK (((is_closed = true) OR ((opens_at IS NOT NULL) AND (closes_at IS NOT NULL)))),
    CONSTRAINT branch_business_hours_weekday_check CHECK (((weekday >= 0) AND (weekday <= 6)))
);


--
-- Name: branch_payment_methods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.branch_payment_methods (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    branch_id uuid NOT NULL,
    payment_flow text NOT NULL,
    method_type text NOT NULL,
    brand text,
    label text NOT NULL,
    icon_key text,
    enabled boolean DEFAULT true NOT NULL,
    requires_gateway boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT branch_payment_methods_method_type_check CHECK ((method_type = ANY (ARRAY['pix'::text, 'credit_card'::text, 'debit_card'::text, 'cash'::text, 'voucher'::text, 'meal_voucher'::text, 'other'::text]))),
    CONSTRAINT branch_payment_methods_payment_flow_check CHECK ((payment_flow = ANY (ARRAY['online'::text, 'delivery'::text])))
);


--
-- Name: branches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.branches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    address text NOT NULL,
    neighborhood text NOT NULL,
    city text NOT NULL,
    state text NOT NULL,
    zipcode text,
    phone text,
    whatsapp text,
    latitude numeric(10,7),
    longitude numeric(10,7),
    is_main boolean DEFAULT false,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    display_name text,
    email text,
    address_street text,
    address_number text,
    address_neighborhood text,
    address_city text,
    address_state text,
    address_zipcode text,
    delivery_base_fee numeric(10,2),
    delivery_fee_per_km numeric(10,2),
    delivery_min_fee numeric(10,2),
    delivery_max_fee numeric(10,2),
    delivery_max_distance_km numeric(10,2),
    CONSTRAINT branches_delivery_fee_config_valid CHECK ((((delivery_base_fee IS NULL) OR (delivery_base_fee >= (0)::numeric)) AND ((delivery_fee_per_km IS NULL) OR (delivery_fee_per_km >= (0)::numeric)) AND ((delivery_min_fee IS NULL) OR (delivery_min_fee >= (0)::numeric)) AND ((delivery_max_fee IS NULL) OR (delivery_max_fee >= (0)::numeric)) AND ((delivery_max_distance_km IS NULL) OR (delivery_max_distance_km > (0)::numeric)) AND ((delivery_min_fee IS NULL) OR (delivery_max_fee IS NULL) OR (delivery_max_fee >= delivery_min_fee))))
);


--
-- Name: cashback_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cashback_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    restaurant_id uuid,
    order_id uuid,
    type text NOT NULL,
    amount numeric(10,2) NOT NULL,
    status text NOT NULL,
    expires_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    idempotency_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.categories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    sort_order integer DEFAULT 0,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: coupon_redemptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.coupon_redemptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    coupon_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    order_id uuid NOT NULL,
    discount_amount numeric(10,2) DEFAULT 0 NOT NULL,
    status text DEFAULT 'applied'::text NOT NULL,
    idempotency_key text,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    reversed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT coupon_redemptions_discount_amount_valid CHECK ((discount_amount >= (0)::numeric)),
    CONSTRAINT coupon_redemptions_reversal_valid CHECK (((status <> 'reversed'::text) OR (reversed_at IS NOT NULL))),
    CONSTRAINT coupon_redemptions_status_valid CHECK ((status = ANY (ARRAY['applied'::text, 'reversed'::text])))
);


--
-- Name: coupon_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.coupon_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    image_path text NOT NULL,
    discount_type text NOT NULL,
    discount_value numeric(10,2) DEFAULT 0 NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT coupon_templates_discount_type_check CHECK ((discount_type = ANY (ARRAY['percent'::text, 'fixed'::text, 'free_delivery'::text]))),
    CONSTRAINT coupon_templates_discount_value_check CHECK ((discount_value >= (0)::numeric))
);


--
-- Name: customer_addresses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_addresses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    label text,
    street text NOT NULL,
    number text,
    neighborhood text,
    complement text,
    reference text,
    city text,
    state text,
    zipcode text,
    latitude numeric,
    longitude numeric,
    is_default boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_reference text
);


--
-- Name: customers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    phone text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    email text,
    cpf text,
    password_hash text,
    birth_date date,
    email_verified_at timestamp with time zone,
    phone_verified_at timestamp with time zone,
    marketing_opt_in boolean DEFAULT false NOT NULL,
    privacy_accepted_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    password_changed_at timestamp with time zone
);


--
-- Name: delivery_estimates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.delivery_estimates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    token text NOT NULL,
    restaurant_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    customer_id uuid,
    address_fingerprint text NOT NULL,
    distance_km numeric(10,2),
    travel_time_min integer,
    prep_time_min integer,
    prep_time_max integer,
    eta_min integer,
    eta_max integer,
    delivery_fee numeric(12,2),
    latitude numeric(10,7),
    longitude numeric(10,7),
    provider text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: email_verification_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_verification_codes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    email text NOT NULL,
    code_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    attempts_count integer DEFAULT 0 NOT NULL,
    resend_count integer DEFAULT 0 NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: idempotency_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.idempotency_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    key text NOT NULL,
    scope text NOT NULL,
    request_fingerprint text NOT NULL,
    status text NOT NULL,
    response_body jsonb,
    order_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_idempotency_keys_status CHECK ((status = ANY (ARRAY['in_progress'::text, 'completed'::text])))
);


--
-- Name: order_item_options; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_item_options (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    order_item_id uuid NOT NULL,
    option_group_id uuid,
    option_id uuid,
    option_group_name_snapshot text NOT NULL,
    option_name_snapshot text NOT NULL,
    additional_price_snapshot numeric DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT order_item_options_price_check CHECK ((additional_price_snapshot >= (0)::numeric))
);


--
-- Name: order_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    order_id uuid NOT NULL,
    product_id uuid,
    product_code_snapshot text,
    product_name_snapshot text NOT NULL,
    product_description_snapshot text,
    unit_price_snapshot numeric(10,2) NOT NULL,
    quantity integer DEFAULT 1 NOT NULL,
    observation text,
    total numeric(10,2) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: order_status_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_status_history (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    order_id uuid NOT NULL,
    status text NOT NULL,
    changed_by text,
    note text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    order_number bigint NOT NULL,
    restaurant_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    customer_id uuid,
    customer_name_snapshot text NOT NULL,
    customer_phone_snapshot text NOT NULL,
    order_type text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    payment_method text,
    subtotal numeric(10,2) DEFAULT 0 NOT NULL,
    delivery_fee numeric(10,2) DEFAULT 0 NOT NULL,
    service_fee numeric(10,2) DEFAULT 0 NOT NULL,
    total numeric(10,2) DEFAULT 0 NOT NULL,
    address_street text,
    address_number text,
    address_neighborhood text,
    address_complement text,
    address_reference text,
    notes text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    customer_address_id uuid,
    address_city text,
    address_state text,
    address_zipcode text,
    delivery_latitude numeric(10,7),
    delivery_longitude numeric(10,7),
    delivery_distance_km numeric(10,2),
    delivery_travel_time_min integer,
    delivery_prep_time_min integer,
    delivery_eta_min integer,
    delivery_eta_max integer,
    delivery_estimate_provider text,
    delivery_estimated_at timestamp with time zone,
    delivery_prep_time_max integer,
    coupon_id uuid,
    coupon_code_snapshot text,
    coupon_discount_amount numeric(10,2) DEFAULT 0 NOT NULL,
    cashback_redeemed_amount numeric(10,2) DEFAULT 0 NOT NULL,
    discount_total numeric(10,2) DEFAULT 0 NOT NULL,
    payment_flow text,
    payment_status text DEFAULT 'on_delivery'::text NOT NULL,
    paid_at timestamp with time zone,
    payment_provider text,
    provider_payment_id text,
    tracking_token text NOT NULL,
    commission_percent numeric(5,2) DEFAULT 0 NOT NULL,
    commission_base_amount numeric(12,2) DEFAULT 0 NOT NULL,
    commission_amount numeric(12,2) DEFAULT 0 NOT NULL,
    CONSTRAINT ck_orders_payment_flow CHECK (((payment_flow IS NULL) OR (payment_flow = ANY (ARRAY['online'::text, 'delivery'::text])))),
    CONSTRAINT ck_orders_payment_status CHECK ((payment_status = ANY (ARRAY['on_delivery'::text, 'pending'::text, 'paid'::text, 'failed'::text, 'refunded'::text]))),
    CONSTRAINT orders_discount_values_valid CHECK (((coupon_discount_amount >= (0)::numeric) AND (cashback_redeemed_amount >= (0)::numeric) AND (discount_total >= (0)::numeric))),
    CONSTRAINT orders_order_type_check CHECK ((order_type = ANY (ARRAY['delivery'::text, 'pickup'::text]))),
    CONSTRAINT orders_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text, 'preparing'::text, 'ready'::text, 'out_for_delivery'::text, 'completed'::text, 'cancelled'::text])))
);


--
-- Name: orders_order_number_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.orders_order_number_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: orders_order_number_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.orders_order_number_seq OWNED BY public.orders.order_number;


--
-- Name: password_reset_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.password_reset_codes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    email text NOT NULL,
    code_hash text NOT NULL,
    reset_token_hash text,
    expires_at timestamp with time zone NOT NULL,
    attempts_count integer DEFAULT 0 NOT NULL,
    resend_count integer DEFAULT 0 NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reset_token_expires_at timestamp with time zone
);


--
-- Name: printing_sectors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.printing_sectors (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    branch_id uuid NOT NULL,
    name text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: product_option_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_option_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    product_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    min_select integer DEFAULT 0 NOT NULL,
    max_select integer DEFAULT 1 NOT NULL,
    is_required boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT product_option_groups_max_check CHECK ((max_select >= 1)),
    CONSTRAINT product_option_groups_min_check CHECK ((min_select >= 0)),
    CONSTRAINT product_option_groups_range_check CHECK ((max_select >= min_select))
);


--
-- Name: product_options; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_options (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    option_group_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    additional_price numeric DEFAULT 0 NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT product_options_price_check CHECK ((additional_price >= (0)::numeric))
);


--
-- Name: products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.products (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    category_id uuid NOT NULL,
    code text,
    name text NOT NULL,
    slug text,
    description text,
    price numeric(10,2) NOT NULL,
    image_path text,
    is_active boolean DEFAULT true,
    is_available boolean DEFAULT true,
    sort_order integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    printing_sector_id uuid
);


--
-- Name: restaurant_banners; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.restaurant_banners (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    image_path text NOT NULL,
    sort_order integer DEFAULT 0,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    banner_type text DEFAULT 'hero'::text NOT NULL,
    CONSTRAINT restaurant_banners_banner_type_check CHECK ((banner_type = ANY (ARRAY['hero'::text, 'highlight'::text])))
);


--
-- Name: restaurant_coupons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.restaurant_coupons (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    coupon_template_id uuid NOT NULL,
    code text NOT NULL,
    min_order_value numeric(10,2) DEFAULT 0 NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    title text NOT NULL,
    description text,
    discount_type text NOT NULL,
    discount_value numeric(10,2) NOT NULL,
    max_discount_amount numeric(10,2),
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    total_usage_limit integer,
    usage_limit_per_customer integer DEFAULT 1,
    first_order_only boolean DEFAULT false NOT NULL,
    is_public boolean DEFAULT true NOT NULL,
    cooldown_days integer,
    CONSTRAINT restaurant_coupons_code_not_blank CHECK ((length(TRIM(BOTH FROM code)) > 0)),
    CONSTRAINT restaurant_coupons_discount_type_valid CHECK ((discount_type = ANY (ARRAY['percent'::text, 'fixed'::text, 'free_delivery'::text]))),
    CONSTRAINT restaurant_coupons_discount_value_valid CHECK ((((discount_type = 'free_delivery'::text) AND (discount_value = (0)::numeric)) OR ((discount_type = ANY (ARRAY['percent'::text, 'fixed'::text])) AND (discount_value > (0)::numeric)))),
    CONSTRAINT restaurant_coupons_max_discount_valid CHECK (((max_discount_amount IS NULL) OR (max_discount_amount > (0)::numeric))),
    CONSTRAINT restaurant_coupons_min_order_value_check CHECK ((min_order_value >= (0)::numeric)),
    CONSTRAINT restaurant_coupons_percent_limit_valid CHECK (((discount_type <> 'percent'::text) OR (discount_value <= (100)::numeric))),
    CONSTRAINT restaurant_coupons_reuse_rules_valid CHECK ((((usage_limit_per_customer IS NULL) OR (usage_limit_per_customer > 0)) AND ((total_usage_limit IS NULL) OR (total_usage_limit > 0)) AND ((cooldown_days IS NULL) OR (cooldown_days > 0)) AND (NOT ((cooldown_days IS NOT NULL) AND (usage_limit_per_customer = 1))))),
    CONSTRAINT restaurant_coupons_validity_range_valid CHECK (((valid_until IS NULL) OR (valid_until > valid_from)))
);


--
-- Name: restaurant_payment_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.restaurant_payment_credentials (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    environment text NOT NULL,
    public_key text NOT NULL,
    access_token_encrypted text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    webhook_secret_encrypted text,
    CONSTRAINT ck_restaurant_payment_credentials_environment CHECK ((environment = ANY (ARRAY['test'::text, 'production'::text])))
);


--
-- Name: restaurant_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.restaurant_settings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    restaurant_id uuid NOT NULL,
    min_order_value numeric(10,2) DEFAULT 0,
    estimated_delivery_time_min integer DEFAULT 30,
    estimated_delivery_time_max integer DEFAULT 60,
    default_delivery_fee numeric(10,2) DEFAULT 0,
    service_fee_enabled boolean DEFAULT true,
    service_fee_amount numeric(10,2) DEFAULT 0.99,
    accepts_delivery boolean DEFAULT true,
    accepts_pickup boolean DEFAULT true,
    payment_methods jsonb DEFAULT '["pix", "credit_card", "debit_card"]'::jsonb,
    is_open boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    platform_commission_percent numeric(5,2) DEFAULT 10.00 NOT NULL,
    CONSTRAINT ck_restaurant_settings_commission_percent CHECK (((platform_commission_percent >= (0)::numeric) AND (platform_commission_percent <= (100)::numeric)))
);


--
-- Name: restaurants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.restaurants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    description text,
    logo_path text,
    cover_path text,
    primary_color text DEFAULT '#D95C04'::text,
    secondary_color text DEFAULT '#111111'::text,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: orders order_number; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders ALTER COLUMN order_number SET DEFAULT nextval('public.orders_order_number_seq'::regclass);


--
-- Name: admin_users admin_users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_email_key UNIQUE (email);


--
-- Name: admin_users admin_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_pkey PRIMARY KEY (id);


--
-- Name: ai_feedback ai_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_feedback
    ADD CONSTRAINT ai_feedback_pkey PRIMARY KEY (id);


--
-- Name: ai_product_embeddings ai_product_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_product_embeddings
    ADD CONSTRAINT ai_product_embeddings_pkey PRIMARY KEY (id);


--
-- Name: ai_product_embeddings ai_product_embeddings_restaurant_product_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_product_embeddings
    ADD CONSTRAINT ai_product_embeddings_restaurant_product_unique UNIQUE (restaurant_id, product_id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: branch_business_hours branch_business_hours_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch_business_hours
    ADD CONSTRAINT branch_business_hours_pkey PRIMARY KEY (id);


--
-- Name: branch_payment_methods branch_payment_methods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch_payment_methods
    ADD CONSTRAINT branch_payment_methods_pkey PRIMARY KEY (id);


--
-- Name: branches branches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branches
    ADD CONSTRAINT branches_pkey PRIMARY KEY (id);


--
-- Name: cashback_transactions cashback_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cashback_transactions
    ADD CONSTRAINT cashback_transactions_pkey PRIMARY KEY (id);


--
-- Name: categories categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_pkey PRIMARY KEY (id);


--
-- Name: coupon_redemptions coupon_redemptions_idempotency_key_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_redemptions
    ADD CONSTRAINT coupon_redemptions_idempotency_key_unique UNIQUE (idempotency_key);


--
-- Name: coupon_redemptions coupon_redemptions_order_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_redemptions
    ADD CONSTRAINT coupon_redemptions_order_unique UNIQUE (order_id);


--
-- Name: coupon_redemptions coupon_redemptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_redemptions
    ADD CONSTRAINT coupon_redemptions_pkey PRIMARY KEY (id);


--
-- Name: coupon_templates coupon_templates_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_templates
    ADD CONSTRAINT coupon_templates_name_key UNIQUE (name);


--
-- Name: coupon_templates coupon_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_templates
    ADD CONSTRAINT coupon_templates_pkey PRIMARY KEY (id);


--
-- Name: customer_addresses customer_addresses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_addresses
    ADD CONSTRAINT customer_addresses_pkey PRIMARY KEY (id);


--
-- Name: customers customers_phone_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_phone_key UNIQUE (phone);


--
-- Name: customers customers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (id);


--
-- Name: delivery_estimates delivery_estimates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_estimates
    ADD CONSTRAINT delivery_estimates_pkey PRIMARY KEY (id);


--
-- Name: email_verification_codes email_verification_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_codes
    ADD CONSTRAINT email_verification_codes_pkey PRIMARY KEY (id);


--
-- Name: idempotency_keys idempotency_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_keys_pkey PRIMARY KEY (id);


--
-- Name: order_item_options order_item_options_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_item_options
    ADD CONSTRAINT order_item_options_pkey PRIMARY KEY (id);


--
-- Name: order_items order_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_pkey PRIMARY KEY (id);


--
-- Name: order_status_history order_status_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_pkey PRIMARY KEY (id);


--
-- Name: orders orders_order_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_order_number_key UNIQUE (order_number);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: password_reset_codes password_reset_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_codes
    ADD CONSTRAINT password_reset_codes_pkey PRIMARY KEY (id);


--
-- Name: printing_sectors printing_sectors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.printing_sectors
    ADD CONSTRAINT printing_sectors_pkey PRIMARY KEY (id);


--
-- Name: product_option_groups product_option_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_option_groups
    ADD CONSTRAINT product_option_groups_pkey PRIMARY KEY (id);


--
-- Name: product_options product_options_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_options
    ADD CONSTRAINT product_options_pkey PRIMARY KEY (id);


--
-- Name: products products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);


--
-- Name: restaurant_banners restaurant_banners_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_banners
    ADD CONSTRAINT restaurant_banners_pkey PRIMARY KEY (id);


--
-- Name: restaurant_coupons restaurant_coupons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_coupons
    ADD CONSTRAINT restaurant_coupons_pkey PRIMARY KEY (id);


--
-- Name: restaurant_payment_credentials restaurant_payment_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_payment_credentials
    ADD CONSTRAINT restaurant_payment_credentials_pkey PRIMARY KEY (id);


--
-- Name: restaurant_settings restaurant_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_settings
    ADD CONSTRAINT restaurant_settings_pkey PRIMARY KEY (id);


--
-- Name: restaurant_settings restaurant_settings_restaurant_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_settings
    ADD CONSTRAINT restaurant_settings_restaurant_id_key UNIQUE (restaurant_id);


--
-- Name: restaurants restaurants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurants
    ADD CONSTRAINT restaurants_pkey PRIMARY KEY (id);


--
-- Name: restaurants restaurants_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurants
    ADD CONSTRAINT restaurants_slug_key UNIQUE (slug);


--
-- Name: delivery_estimates uq_delivery_estimates_token; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_estimates
    ADD CONSTRAINT uq_delivery_estimates_token UNIQUE (token);


--
-- Name: idempotency_keys uq_idempotency_keys_scope_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT uq_idempotency_keys_scope_key UNIQUE (scope, key);


--
-- Name: orders uq_orders_tracking_token; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT uq_orders_tracking_token UNIQUE (tracking_token);


--
-- Name: printing_sectors uq_printing_sectors_branch_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.printing_sectors
    ADD CONSTRAINT uq_printing_sectors_branch_name UNIQUE (branch_id, name);


--
-- Name: restaurant_payment_credentials uq_restaurant_payment_credentials_restaurant_environment; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_payment_credentials
    ADD CONSTRAINT uq_restaurant_payment_credentials_restaurant_environment UNIQUE (restaurant_id, environment);


--
-- Name: idx_admin_users_restaurant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_users_restaurant_id ON public.admin_users USING btree (restaurant_id);


--
-- Name: idx_ai_feedback_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_feedback_created ON public.ai_feedback USING btree (created_at DESC);


--
-- Name: idx_ai_feedback_restaurant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_feedback_restaurant ON public.ai_feedback USING btree (restaurant_id);


--
-- Name: idx_branch_business_hours_branch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branch_business_hours_branch_id ON public.branch_business_hours USING btree (branch_id);


--
-- Name: idx_branch_business_hours_weekday; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branch_business_hours_weekday ON public.branch_business_hours USING btree (branch_id, weekday, sort_order);


--
-- Name: idx_branch_payment_methods_branch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branch_payment_methods_branch_id ON public.branch_payment_methods USING btree (branch_id);


--
-- Name: idx_branch_payment_methods_flow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branch_payment_methods_flow ON public.branch_payment_methods USING btree (branch_id, payment_flow, enabled);


--
-- Name: idx_branch_payment_methods_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_branch_payment_methods_unique ON public.branch_payment_methods USING btree (branch_id, payment_flow, method_type, COALESCE(brand, ''::text));


--
-- Name: idx_branches_restaurant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branches_restaurant_id ON public.branches USING btree (restaurant_id);


--
-- Name: idx_cashback_transactions_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cashback_transactions_created_at ON public.cashback_transactions USING btree (created_at);


--
-- Name: idx_cashback_transactions_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cashback_transactions_customer_id ON public.cashback_transactions USING btree (customer_id);


--
-- Name: idx_cashback_transactions_customer_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cashback_transactions_customer_status ON public.cashback_transactions USING btree (customer_id, status);


--
-- Name: idx_cashback_transactions_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_cashback_transactions_idempotency_key ON public.cashback_transactions USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: idx_categories_restaurant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_categories_restaurant_id ON public.categories USING btree (restaurant_id);


--
-- Name: idx_coupon_redemptions_applied; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coupon_redemptions_applied ON public.coupon_redemptions USING btree (coupon_id, customer_id) WHERE (status = 'applied'::text);


--
-- Name: idx_coupon_redemptions_coupon_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coupon_redemptions_coupon_status ON public.coupon_redemptions USING btree (coupon_id, status);


--
-- Name: idx_coupon_redemptions_customer_coupon_applied_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coupon_redemptions_customer_coupon_applied_at ON public.coupon_redemptions USING btree (customer_id, coupon_id, applied_at DESC) WHERE (status = 'applied'::text);


--
-- Name: idx_coupon_redemptions_customer_coupon_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coupon_redemptions_customer_coupon_status ON public.coupon_redemptions USING btree (customer_id, coupon_id, status);


--
-- Name: idx_coupon_templates_active_sort; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coupon_templates_active_sort ON public.coupon_templates USING btree (is_active, sort_order);


--
-- Name: idx_customer_addresses_client_reference; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_customer_addresses_client_reference ON public.customer_addresses USING btree (customer_id, client_reference);


--
-- Name: idx_customer_addresses_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_customer_addresses_customer_id ON public.customer_addresses USING btree (customer_id);


--
-- Name: idx_customer_addresses_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_customer_addresses_default ON public.customer_addresses USING btree (customer_id, is_default);


--
-- Name: idx_customers_cpf_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_customers_cpf_unique ON public.customers USING btree (cpf) WHERE (cpf IS NOT NULL);


--
-- Name: idx_customers_email_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_customers_email_unique ON public.customers USING btree (lower(email)) WHERE (email IS NOT NULL);


--
-- Name: idx_customers_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_customers_phone ON public.customers USING btree (phone);


--
-- Name: idx_email_verification_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_email_verification_active ON public.email_verification_codes USING btree (email, used_at, expires_at);


--
-- Name: idx_email_verification_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_email_verification_customer_id ON public.email_verification_codes USING btree (customer_id);


--
-- Name: idx_email_verification_email_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_email_verification_email_created_at ON public.email_verification_codes USING btree (email, created_at DESC);


--
-- Name: idx_order_item_options_order_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_order_item_options_order_item_id ON public.order_item_options USING btree (order_item_id);


--
-- Name: idx_order_status_history_order_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_order_status_history_order_id ON public.order_status_history USING btree (order_id);


--
-- Name: idx_orders_branch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_branch_id ON public.orders USING btree (branch_id);


--
-- Name: idx_orders_coupon_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_coupon_id ON public.orders USING btree (coupon_id) WHERE (coupon_id IS NOT NULL);


--
-- Name: idx_orders_customer_address_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_customer_address_id ON public.orders USING btree (customer_address_id);


--
-- Name: idx_orders_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_customer_id ON public.orders USING btree (customer_id);


--
-- Name: idx_orders_customer_id_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_customer_id_created_at ON public.orders USING btree (customer_id, created_at DESC);


--
-- Name: idx_orders_restaurant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_restaurant_id ON public.orders USING btree (restaurant_id);


--
-- Name: idx_orders_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_status ON public.orders USING btree (status);


--
-- Name: idx_password_reset_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_password_reset_active ON public.password_reset_codes USING btree (email, used_at, expires_at);


--
-- Name: idx_password_reset_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_password_reset_customer_id ON public.password_reset_codes USING btree (customer_id);


--
-- Name: idx_password_reset_email_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_password_reset_email_created_at ON public.password_reset_codes USING btree (email, created_at DESC);


--
-- Name: idx_product_option_groups_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_product_option_groups_product_id ON public.product_option_groups USING btree (product_id);


--
-- Name: idx_product_options_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_product_options_group_id ON public.product_options USING btree (option_group_id);


--
-- Name: idx_products_category_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_products_category_id ON public.products USING btree (category_id);


--
-- Name: idx_products_restaurant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_products_restaurant_id ON public.products USING btree (restaurant_id);


--
-- Name: idx_restaurant_banners_restaurant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restaurant_banners_restaurant_id ON public.restaurant_banners USING btree (restaurant_id);


--
-- Name: idx_restaurant_banners_restaurant_type_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restaurant_banners_restaurant_type_active ON public.restaurant_banners USING btree (restaurant_id, banner_type, is_active, sort_order);


--
-- Name: idx_restaurant_coupons_public_active_validity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restaurant_coupons_public_active_validity ON public.restaurant_coupons USING btree (restaurant_id, is_active, is_public, valid_from, valid_until);


--
-- Name: idx_restaurant_coupons_restaurant_active_sort; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restaurant_coupons_restaurant_active_sort ON public.restaurant_coupons USING btree (restaurant_id, is_active, sort_order);


--
-- Name: ix_admin_users_restaurant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_admin_users_restaurant_id ON public.admin_users USING btree (restaurant_id);


--
-- Name: ix_delivery_estimates_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_delivery_estimates_expires_at ON public.delivery_estimates USING btree (expires_at);


--
-- Name: ix_idempotency_keys_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idempotency_keys_expires_at ON public.idempotency_keys USING btree (expires_at);


--
-- Name: ix_order_items_order_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_order_items_order_id ON public.order_items USING btree (order_id);


--
-- Name: ix_order_status_history_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_order_status_history_created_at ON public.order_status_history USING btree (created_at);


--
-- Name: ix_orders_restaurant_branch_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_restaurant_branch_created_at ON public.orders USING btree (restaurant_id, branch_id, created_at);


--
-- Name: ix_orders_restaurant_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_restaurant_created_at ON public.orders USING btree (restaurant_id, created_at);


--
-- Name: ix_orders_restaurant_customer_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_restaurant_customer_phone ON public.orders USING btree (restaurant_id, customer_phone_snapshot);


--
-- Name: ix_printing_sectors_branch_sort_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_printing_sectors_branch_sort_order ON public.printing_sectors USING btree (branch_id, sort_order);


--
-- Name: ix_products_printing_sector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_products_printing_sector ON public.products USING btree (printing_sector_id);


--
-- Name: ix_products_restaurant_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_products_restaurant_category ON public.products USING btree (restaurant_id, category_id);


--
-- Name: restaurant_coupons_restaurant_code_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX restaurant_coupons_restaurant_code_unique ON public.restaurant_coupons USING btree (restaurant_id, code);


--
-- Name: restaurant_coupons_restaurant_template_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX restaurant_coupons_restaurant_template_unique ON public.restaurant_coupons USING btree (restaurant_id, coupon_template_id);


--
-- Name: uq_admin_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_admin_users_email ON public.admin_users USING btree (lower(email));


--
-- Name: uq_categories_restaurant_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_categories_restaurant_slug ON public.categories USING btree (restaurant_id, slug);


--
-- Name: uq_orders_provider_payment; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_orders_provider_payment ON public.orders USING btree (payment_provider, provider_payment_id) WHERE (provider_payment_id IS NOT NULL);


--
-- Name: uq_products_restaurant_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_products_restaurant_slug ON public.products USING btree (restaurant_id, slug);


--
-- Name: uq_restaurant_banners_restaurant_image_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_restaurant_banners_restaurant_image_path ON public.restaurant_banners USING btree (restaurant_id, image_path);


--
-- Name: uq_restaurant_coupons_restaurant_code_ci; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_restaurant_coupons_restaurant_code_ci ON public.restaurant_coupons USING btree (restaurant_id, lower(TRIM(BOTH FROM code)));


--
-- Name: admin_users trg_admin_users_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_admin_users_updated_at BEFORE UPDATE ON public.admin_users FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: branches trg_branches_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_branches_updated_at BEFORE UPDATE ON public.branches FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: categories trg_categories_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_categories_updated_at BEFORE UPDATE ON public.categories FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: customers trg_customers_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_customers_updated_at BEFORE UPDATE ON public.customers FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: orders trg_orders_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_orders_updated_at BEFORE UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: products trg_products_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_products_updated_at BEFORE UPDATE ON public.products FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: restaurant_banners trg_restaurant_banners_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_restaurant_banners_updated_at BEFORE UPDATE ON public.restaurant_banners FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: restaurant_settings trg_restaurant_settings_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_restaurant_settings_updated_at BEFORE UPDATE ON public.restaurant_settings FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: restaurants trg_restaurants_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_restaurants_updated_at BEFORE UPDATE ON public.restaurants FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: admin_users admin_users_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branches(id) ON DELETE SET NULL;


--
-- Name: admin_users admin_users_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: ai_feedback ai_feedback_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_feedback
    ADD CONSTRAINT ai_feedback_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id);


--
-- Name: ai_product_embeddings ai_product_embeddings_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_product_embeddings
    ADD CONSTRAINT ai_product_embeddings_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id);


--
-- Name: ai_product_embeddings ai_product_embeddings_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_product_embeddings
    ADD CONSTRAINT ai_product_embeddings_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id);


--
-- Name: branch_business_hours branch_business_hours_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch_business_hours
    ADD CONSTRAINT branch_business_hours_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branches(id) ON DELETE CASCADE;


--
-- Name: branch_payment_methods branch_payment_methods_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branch_payment_methods
    ADD CONSTRAINT branch_payment_methods_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branches(id) ON DELETE CASCADE;


--
-- Name: branches branches_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branches
    ADD CONSTRAINT branches_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: cashback_transactions cashback_transactions_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cashback_transactions
    ADD CONSTRAINT cashback_transactions_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE CASCADE;


--
-- Name: cashback_transactions cashback_transactions_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cashback_transactions
    ADD CONSTRAINT cashback_transactions_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE SET NULL;


--
-- Name: cashback_transactions cashback_transactions_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cashback_transactions
    ADD CONSTRAINT cashback_transactions_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE SET NULL;


--
-- Name: categories categories_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: coupon_redemptions coupon_redemptions_coupon_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_redemptions
    ADD CONSTRAINT coupon_redemptions_coupon_id_fkey FOREIGN KEY (coupon_id) REFERENCES public.restaurant_coupons(id) ON DELETE RESTRICT;


--
-- Name: coupon_redemptions coupon_redemptions_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_redemptions
    ADD CONSTRAINT coupon_redemptions_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE RESTRICT;


--
-- Name: coupon_redemptions coupon_redemptions_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coupon_redemptions
    ADD CONSTRAINT coupon_redemptions_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: customer_addresses customer_addresses_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_addresses
    ADD CONSTRAINT customer_addresses_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE CASCADE;


--
-- Name: delivery_estimates delivery_estimates_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_estimates
    ADD CONSTRAINT delivery_estimates_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branches(id);


--
-- Name: delivery_estimates delivery_estimates_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_estimates
    ADD CONSTRAINT delivery_estimates_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: delivery_estimates delivery_estimates_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_estimates
    ADD CONSTRAINT delivery_estimates_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id);


--
-- Name: email_verification_codes email_verification_codes_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_codes
    ADD CONSTRAINT email_verification_codes_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE CASCADE;


--
-- Name: idempotency_keys idempotency_keys_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_keys_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE SET NULL;


--
-- Name: order_item_options order_item_options_option_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_item_options
    ADD CONSTRAINT order_item_options_option_group_id_fkey FOREIGN KEY (option_group_id) REFERENCES public.product_option_groups(id);


--
-- Name: order_item_options order_item_options_option_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_item_options
    ADD CONSTRAINT order_item_options_option_id_fkey FOREIGN KEY (option_id) REFERENCES public.product_options(id);


--
-- Name: order_item_options order_item_options_order_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_item_options
    ADD CONSTRAINT order_item_options_order_item_id_fkey FOREIGN KEY (order_item_id) REFERENCES public.order_items(id) ON DELETE CASCADE;


--
-- Name: order_items order_items_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: order_items order_items_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id);


--
-- Name: order_status_history order_status_history_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: orders orders_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branches(id);


--
-- Name: orders orders_coupon_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_coupon_id_fkey FOREIGN KEY (coupon_id) REFERENCES public.restaurant_coupons(id) ON DELETE SET NULL;


--
-- Name: orders orders_customer_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_customer_address_id_fkey FOREIGN KEY (customer_address_id) REFERENCES public.customer_addresses(id) ON DELETE SET NULL;


--
-- Name: orders orders_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id);


--
-- Name: orders orders_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id);


--
-- Name: password_reset_codes password_reset_codes_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_codes
    ADD CONSTRAINT password_reset_codes_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE CASCADE;


--
-- Name: printing_sectors printing_sectors_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.printing_sectors
    ADD CONSTRAINT printing_sectors_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branches(id);


--
-- Name: product_option_groups product_option_groups_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_option_groups
    ADD CONSTRAINT product_option_groups_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_options product_options_option_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_options
    ADD CONSTRAINT product_options_option_group_id_fkey FOREIGN KEY (option_group_id) REFERENCES public.product_option_groups(id) ON DELETE CASCADE;


--
-- Name: products products_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id) ON DELETE CASCADE;


--
-- Name: products products_printing_sector_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_printing_sector_id_fkey FOREIGN KEY (printing_sector_id) REFERENCES public.printing_sectors(id);


--
-- Name: products products_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: restaurant_banners restaurant_banners_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_banners
    ADD CONSTRAINT restaurant_banners_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: restaurant_coupons restaurant_coupons_coupon_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_coupons
    ADD CONSTRAINT restaurant_coupons_coupon_template_id_fkey FOREIGN KEY (coupon_template_id) REFERENCES public.coupon_templates(id);


--
-- Name: restaurant_coupons restaurant_coupons_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_coupons
    ADD CONSTRAINT restaurant_coupons_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: restaurant_payment_credentials restaurant_payment_credentials_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_payment_credentials
    ADD CONSTRAINT restaurant_payment_credentials_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: restaurant_settings restaurant_settings_restaurant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restaurant_settings
    ADD CONSTRAINT restaurant_settings_restaurant_id_fkey FOREIGN KEY (restaurant_id) REFERENCES public.restaurants(id) ON DELETE CASCADE;


--
-- Name: admin_users; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.admin_users ENABLE ROW LEVEL SECURITY;

--
-- Name: ai_feedback; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_feedback ENABLE ROW LEVEL SECURITY;

--
-- Name: ai_product_embeddings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_product_embeddings ENABLE ROW LEVEL SECURITY;

--
-- Name: alembic_version; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY;

--
-- Name: branch_business_hours; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.branch_business_hours ENABLE ROW LEVEL SECURITY;

--
-- Name: branch_payment_methods; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.branch_payment_methods ENABLE ROW LEVEL SECURITY;

--
-- Name: branches; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.branches ENABLE ROW LEVEL SECURITY;

--
-- Name: cashback_transactions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.cashback_transactions ENABLE ROW LEVEL SECURITY;

--
-- Name: categories; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.categories ENABLE ROW LEVEL SECURITY;

--
-- Name: coupon_redemptions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.coupon_redemptions ENABLE ROW LEVEL SECURITY;

--
-- Name: coupon_templates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.coupon_templates ENABLE ROW LEVEL SECURITY;

--
-- Name: customer_addresses; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.customer_addresses ENABLE ROW LEVEL SECURITY;

--
-- Name: customers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;

--
-- Name: delivery_estimates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.delivery_estimates ENABLE ROW LEVEL SECURITY;

--
-- Name: email_verification_codes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.email_verification_codes ENABLE ROW LEVEL SECURITY;

--
-- Name: idempotency_keys; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.idempotency_keys ENABLE ROW LEVEL SECURITY;

--
-- Name: order_item_options; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.order_item_options ENABLE ROW LEVEL SECURITY;

--
-- Name: order_items; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.order_items ENABLE ROW LEVEL SECURITY;

--
-- Name: order_status_history; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.order_status_history ENABLE ROW LEVEL SECURITY;

--
-- Name: orders; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;

--
-- Name: password_reset_codes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.password_reset_codes ENABLE ROW LEVEL SECURITY;

--
-- Name: printing_sectors; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.printing_sectors ENABLE ROW LEVEL SECURITY;

--
-- Name: product_option_groups; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.product_option_groups ENABLE ROW LEVEL SECURITY;

--
-- Name: product_options; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.product_options ENABLE ROW LEVEL SECURITY;

--
-- Name: products; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;

--
-- Name: restaurant_banners; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.restaurant_banners ENABLE ROW LEVEL SECURITY;

--
-- Name: restaurant_coupons; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.restaurant_coupons ENABLE ROW LEVEL SECURITY;

--
-- Name: restaurant_payment_credentials; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.restaurant_payment_credentials ENABLE ROW LEVEL SECURITY;

--
-- Name: restaurant_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.restaurant_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: restaurants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.restaurants ENABLE ROW LEVEL SECURITY;

--
-- PostgreSQL database dump complete
--



--
-- Name: ensure_rls; Type: EVENT TRIGGER; Schema: -; Owner: -
--
-- ACRESCENTADO A MAO EM 12/08/2026 (achado A-baseline da auditoria).
--
-- O `pg_dump --schema-only` que gerou este arquivo trouxe a FUNCAO
-- `rls_auto_enable()` la em cima, mas nao o gatilho que a dispara. Sem esta
-- linha, um banco montado so a partir deste arquivo — que e exatamente o que
-- a fixture da suite `db` faz — ganha a funcao e nenhuma automacao: tabela
-- criada por revisao futura do Alembic nasceria SEM RLS no teste e COM RLS em
-- producao. O teste passaria verde contra um banco que nao e o de producao,
-- que e a armadilha 33 aparecendo de novo por outra porta.
--
-- Fica no FIM do arquivo, e nao junto da funcao, de proposito: aqui em cima
-- ele dispararia a cada CREATE TABLE do proprio baseline, refazendo um
-- trabalho que os `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` acima ja
-- fizeram explicitamente. No fim, ele passa a valer para o que vier DEPOIS —
-- que e a unica coisa que ele precisa cobrir.
--
-- Os tres command tags e o `ddl_command_end` sao os de producao, conferidos
-- em `pg_event_trigger` no dia.
--

CREATE EVENT TRIGGER ensure_rls ON ddl_command_end
   WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
   EXECUTE FUNCTION public.rls_auto_enable();
