create extension if not exists vector with schema extensions;

-- Accounts: one per agent identity. Balance is denormalized for O(1) reads,
-- always mutated in the same transaction as a wallet_ledger insert.
create table public.accounts (
  id uuid primary key default gen_random_uuid(),
  label text,
  balance_credits numeric(14,6) not null default 0,
  created_at timestamptz not null default now()
);
alter table public.accounts enable row level security;

-- API keys: only a sha256 hash is stored, never the plaintext key.
create table public.api_keys (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references public.accounts(id) on delete cascade,
  key_hash text not null unique,
  key_prefix text not null,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked boolean not null default false
);
alter table public.api_keys enable row level security;
create index api_keys_account_idx on public.api_keys(account_id);

-- Scars: one recorded failure each, with a semantic embedding for matching.
create table public.scars (
  id uuid primary key default gen_random_uuid(),
  action_signature text not null,
  signature_sha text not null,
  failure_mode text not null,
  context jsonb not null default '{}'::jsonb,
  evidence text not null,
  embedding extensions.vector(384),
  submitted_by uuid references public.accounts(id) on delete set null,
  submitted_at timestamptz not null default now(),
  confirmations int not null default 1,
  last_confirmed_at timestamptz
);
alter table public.scars enable row level security;
create unique index scars_dedupe_idx on public.scars (signature_sha, lower(failure_mode));
create index scars_embedding_idx on public.scars using hnsw (embedding extensions.vector_cosine_ops);

-- Append-only ledger. Account balance = running sum of delta_credits.
create table public.wallet_ledger (
  id bigint generated always as identity primary key,
  account_id uuid not null references public.accounts(id) on delete cascade,
  delta_credits numeric(14,6) not null,
  reason text not null,
  ref_id text,
  created_at timestamptz not null default now()
);
alter table public.wallet_ledger enable row level security;
create index wallet_ledger_account_idx on public.wallet_ledger(account_id, created_at desc);

-- Reverse-auction bounties.
create table public.bounties (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references public.accounts(id) on delete cascade,
  action_signature text not null,
  embedding extensions.vector(384),
  max_pay_credits numeric(14,6) not null,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
alter table public.bounties enable row level security;
create index bounties_active_idx on public.bounties(expires_at);

-- Note on security model: RLS is enabled with NO policies on every table.
-- The Data API (PostgREST) therefore returns zero rows to anon/authenticated.
-- All access flows through the `scar` Edge Function using the service_role,
-- which bypasses RLS, after authenticating the caller's Scar API key.
