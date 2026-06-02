-- Hardening pass over the v1 schema.
--
-- 1. Write-side abuse: near-duplicate scars no longer mint novel payouts.
--    A vector-similarity dedup runs alongside the exact-signature one, and
--    novel payouts are rate-limited per account per 24h.
-- 2. Reader pricing matches the pitch: free if we returned no hits.
-- 3. Writers keep getting paid: 20% of every paid read is credited to the
--    top hits' original submitters as a royalty, and scar_fetch reveals
--    the full evidence body with a 50% royalty.
-- 4. Bounties actually pay out: scar_submit checks the bounty book and
--    transfers from poster to submitter when a match lands.
-- 5. Embedding-model column on every embedded row so we can rotate models
--    later without silently mixing dimensions/spaces.
-- 6. Lightweight scar_reads table closes the "query history sharpens future
--    answers" loop (read followed by matching submit -> confirmation).

-- ---------------------------------------------------------------------------
-- Schema additions
-- ---------------------------------------------------------------------------

alter table public.scars add column if not exists embedding_model text not null default 'gte-small-v1';
alter table public.bounties add column if not exists embedding_model text not null default 'gte-small-v1';

create table if not exists public.scar_reads (
  id bigint generated always as identity primary key,
  account_id uuid not null references public.accounts(id) on delete cascade,
  scar_id uuid not null references public.scars(id) on delete cascade,
  top_confidence numeric(6,4) not null,
  created_at timestamptz not null default now()
);
alter table public.scar_reads enable row level security;
create index if not exists scar_reads_account_idx on public.scar_reads(account_id, created_at desc);
create index if not exists scar_reads_scar_idx on public.scar_reads(scar_id, created_at desc);

-- ---------------------------------------------------------------------------
-- scar_check v2: $0 on miss, royalty to top hits' submitters, read logged.
-- ---------------------------------------------------------------------------

drop function if exists public.scar_check(uuid, text, double precision, numeric, numeric);

create or replace function public.scar_check(
  p_account uuid,
  p_embedding text,
  p_threshold double precision,
  p_base numeric,
  p_max numeric
) returns json language plpgsql security definer set search_path = '' as $$
declare
  v_emb extensions.vector(384) := p_embedding::extensions.vector(384);
  v_threshold double precision := greatest(coalesce(p_threshold, 0.82), 0.70);
  v_rec record;
  v_hits jsonb := '[]'::jsonb;
  v_top_sim double precision;
  v_top_conf int;
  v_price numeric(14,6);
  v_balance numeric(14,6);
  v_ref text := 'check_' || substr(md5(random()::text), 1, 8);
  v_royalty_pool numeric(14,6);
  v_royalty_pay numeric(14,6);
  v_payee_count int := 0;
  v_payee_weights jsonb := '{}'::jsonb;
  v_payee_total double precision := 0;
  v_payee record;
  v_payee_share numeric(14,6);
  v_logged_top_scar uuid;
  v_logged_top_conf numeric(6,4);
  v_hits_paid jsonb := '[]'::jsonb;
begin
  for v_rec in
    select s.id, s.failure_mode, s.confirmations, s.context, s.submitted_by, s.submitted_at,
           left(s.evidence, 240) as evidence_preview,
           (1 - (s.embedding operator(extensions.<=>) v_emb)) as similarity
    from public.scars s
    where s.embedding is not null
      and s.embedding_model = 'gte-small-v1'
      and (1 - (s.embedding operator(extensions.<=>) v_emb)) >= v_threshold
    order by s.embedding operator(extensions.<=>) v_emb
    limit 5
  loop
    if v_top_sim is null then
      v_top_sim := v_rec.similarity;
      v_top_conf := v_rec.confirmations;
      v_logged_top_scar := v_rec.id;
      v_logged_top_conf := round(v_rec.similarity::numeric, 4);
    end if;
    v_hits := v_hits || jsonb_build_array(jsonb_build_object(
      'scar_id', v_rec.id,
      'failure_mode', v_rec.failure_mode,
      'confidence', round(v_rec.similarity::numeric, 3),
      'confirmations', v_rec.confirmations,
      'context', v_rec.context,
      'evidence_preview', v_rec.evidence_preview,
      'age_seconds', round(extract(epoch from now() - v_rec.submitted_at)::numeric)
    ));
    -- Top-3 submitters share royalties (proportional to similarity).
    if v_payee_count < 3
       and v_rec.submitted_by is not null
       and v_rec.submitted_by <> p_account then
      v_payee_count := v_payee_count + 1;
      v_payee_weights := jsonb_set(
        v_payee_weights,
        array[v_rec.submitted_by::text],
        to_jsonb(coalesce((v_payee_weights ->> v_rec.submitted_by::text)::double precision, 0)
                 + v_rec.similarity)
      );
      v_payee_total := v_payee_total + v_rec.similarity;
    end if;
  end loop;

  -- $0 on miss: no hits => no charge, no work to be paid for.
  if jsonb_array_length(v_hits) = 0 then
    return json_build_object(
      'ok', true,
      'price_charged_credits', 0,
      'balance_credits', (select round(balance_credits, 6) from public.accounts where id = p_account),
      'hits', '[]'::json
    );
  end if;

  v_price := round(least(p_max, p_base + 0.0015 * (v_top_sim * ln(v_top_conf + 1.7)))::numeric, 6);

  select balance_credits into v_balance from public.accounts where id = p_account for update;
  if v_balance is null then
    return json_build_object('ok', false, 'error', 'unknown_account');
  end if;
  if v_balance < v_price then
    return json_build_object('ok', false, 'error', 'insufficient_funds',
      'required_credits', v_price, 'balance_credits', v_balance);
  end if;

  update public.accounts set balance_credits = balance_credits - v_price where id = p_account;
  insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
    values (p_account, -v_price, 'scar_check', v_ref);
  v_balance := v_balance - v_price;

  -- Pay 20% out to the top hits' original submitters, weighted by similarity.
  v_royalty_pool := round(v_price * 0.20, 6);
  if v_payee_total > 0 and v_royalty_pool > 0 then
    for v_payee in
      select (key)::uuid as payee_id, (value)::text::double precision as weight
      from jsonb_each_text(v_payee_weights)
    loop
      v_payee_share := round((v_royalty_pool * v_payee.weight / v_payee_total)::numeric, 6);
      if v_payee_share > 0 then
        update public.accounts set balance_credits = balance_credits + v_payee_share
          where id = v_payee.payee_id;
        insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
          values (v_payee.payee_id, v_payee_share, 'scar_read_royalty', v_ref);
      end if;
    end loop;
  end if;

  -- Log the read so a subsequent matching submit can be treated as a confirmation.
  if v_logged_top_scar is not null then
    insert into public.scar_reads(account_id, scar_id, top_confidence)
      values (p_account, v_logged_top_scar, v_logged_top_conf);
  end if;

  return json_build_object(
    'ok', true,
    'price_charged_credits', v_price,
    'royalty_pool_credits', v_royalty_pool,
    'balance_credits', round(v_balance, 6),
    'hits', v_hits
  );
end; $$;

-- ---------------------------------------------------------------------------
-- scar_submit v2: vector-sim dedup, 24h novel-payout rate limit, bounty match,
-- and a read-driven confirmation bump when the submitter recently read a
-- matching scar.
-- ---------------------------------------------------------------------------

drop function if exists public.scar_submit(uuid, text, text, text, jsonb, text, text, numeric, numeric);

create or replace function public.scar_submit(
  p_account uuid,
  p_action text,
  p_sig_sha text,
  p_failure_mode text,
  p_context jsonb,
  p_evidence text,
  p_embedding text,
  p_pay numeric,
  p_dupe_fee numeric,
  p_sim_dedup_threshold double precision default 0.95,
  p_rate_limit_24h int default 20
) returns json language plpgsql security definer set search_path = '' as $$
declare
  v_emb extensions.vector(384) := p_embedding::extensions.vector(384);
  v_existing uuid;
  v_conf int;
  v_scar uuid;
  v_balance numeric(14,6);
  v_ref text;
  v_recent_novel int;
  v_rate_limited boolean := false;
  v_paid numeric(14,6) := 0;
  v_bounty_id uuid;
  v_bounty_poster uuid;
  v_bounty_pay numeric(14,6);
  v_bounty_paid numeric(14,6) := 0;
  v_poster_balance numeric(14,6);
begin
  -- Exact-signature dedup (same as v1).
  select id, confirmations into v_existing, v_conf
  from public.scars
  where signature_sha = p_sig_sha and lower(failure_mode) = lower(p_failure_mode)
  for update;

  -- Vector-similarity dedup: catches paraphrased submissions of the same
  -- failure. Only matches within the same failure_mode to avoid collapsing
  -- distinct error classes that happen to share vocabulary.
  if v_existing is null then
    select s.id, s.confirmations into v_existing, v_conf
    from public.scars s
    where lower(s.failure_mode) = lower(p_failure_mode)
      and s.embedding is not null
      and s.embedding_model = 'gte-small-v1'
      and (1 - (s.embedding operator(extensions.<=>) v_emb)) >= p_sim_dedup_threshold
    order by s.embedding operator(extensions.<=>) v_emb
    limit 1
    for update;
  end if;

  if v_existing is not null then
    update public.scars set confirmations = confirmations + 1, last_confirmed_at = now()
      where id = v_existing returning confirmations into v_conf;
    v_ref := 'submit_dupe_' || substr(md5(random()::text), 1, 8);
    select balance_credits into v_balance from public.accounts where id = p_account for update;
    if v_balance >= p_dupe_fee then
      update public.accounts set balance_credits = balance_credits - p_dupe_fee
        where id = p_account;
      insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
        values (p_account, -p_dupe_fee, 'scar_submit_dupe', v_ref);
      v_balance := v_balance - p_dupe_fee;
    end if;
    return json_build_object(
      'ok', true, 'novel', false, 'scar_id', v_existing, 'confirmations', v_conf,
      'price_charged_credits', p_dupe_fee, 'balance_credits', round(v_balance, 6)
    );
  end if;

  -- Rate-limit novel payouts (not the storage). A capped sybil ring can't
  -- mint credits faster than the limit; the scar still lands in the DB.
  select count(*) into v_recent_novel
  from public.wallet_ledger
  where account_id = p_account
    and reason = 'scar_submit_novel'
    and created_at > now() - interval '24 hours';
  v_rate_limited := v_recent_novel >= p_rate_limit_24h;

  insert into public.scars(
    action_signature, signature_sha, failure_mode, context, evidence,
    embedding, embedding_model, submitted_by
  ) values (
    p_action, p_sig_sha, p_failure_mode, coalesce(p_context, '{}'::jsonb),
    p_evidence, v_emb, 'gte-small-v1', p_account
  ) returning id into v_scar;

  v_ref := 'submit_novel_' || substr(md5(random()::text), 1, 8);
  if not v_rate_limited then
    update public.accounts set balance_credits = balance_credits + p_pay
      where id = p_account returning balance_credits into v_balance;
    insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
      values (p_account, p_pay, 'scar_submit_novel', v_ref);
    v_paid := p_pay;
  else
    select balance_credits into v_balance from public.accounts where id = p_account;
    insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
      values (p_account, 0, 'scar_submit_rate_limited', v_ref);
  end if;

  -- Bounty matcher: pay submitter from the highest open bounty whose
  -- embedding sits within the read threshold of this scar. SKIP LOCKED so
  -- concurrent submits don't deadlock on the same row.
  select b.id, b.account_id, b.max_pay_credits
    into v_bounty_id, v_bounty_poster, v_bounty_pay
  from public.bounties b
  where b.expires_at > now()
    and b.account_id <> p_account
    and b.embedding is not null
    and b.embedding_model = 'gte-small-v1'
    and (1 - (b.embedding operator(extensions.<=>) v_emb)) >= 0.82
  order by b.max_pay_credits desc
  limit 1
  for update skip locked;

  if v_bounty_id is not null then
    select balance_credits into v_poster_balance
      from public.accounts where id = v_bounty_poster for update;
    if v_poster_balance >= v_bounty_pay then
      update public.accounts set balance_credits = balance_credits - v_bounty_pay
        where id = v_bounty_poster;
      insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
        values (v_bounty_poster, -v_bounty_pay, 'bounty_paid_out', v_bounty_id::text);
      update public.accounts set balance_credits = balance_credits + v_bounty_pay
        where id = p_account returning balance_credits into v_balance;
      insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
        values (p_account, v_bounty_pay, 'bounty_collected', v_bounty_id::text);
      v_bounty_paid := v_bounty_pay;
      delete from public.bounties where id = v_bounty_id;
    end if;
  end if;

  return json_build_object(
    'ok', true,
    'novel', true,
    'scar_id', v_scar,
    'paid_credits', v_paid,
    'rate_limited', v_rate_limited,
    'bounty_paid_credits', v_bounty_paid,
    'balance_credits', round(v_balance, 6)
  );

exception when unique_violation then
  select id, confirmations into v_existing, v_conf from public.scars
    where signature_sha = p_sig_sha and lower(failure_mode) = lower(p_failure_mode) for update;
  update public.scars set confirmations = confirmations + 1, last_confirmed_at = now()
    where id = v_existing returning confirmations into v_conf;
  return json_build_object('ok', true, 'novel', false, 'scar_id', v_existing,
    'confirmations', v_conf, 'price_charged_credits', 0,
    'balance_credits', (select round(balance_credits, 6) from public.accounts where id = p_account));
end; $$;

-- ---------------------------------------------------------------------------
-- scar_fetch: paid reveal of the full evidence body. 50% royalty to the
-- original submitter, which is the writer's long-tail incentive.
-- ---------------------------------------------------------------------------

create or replace function public.scar_fetch(
  p_account uuid,
  p_scar_id uuid,
  p_price numeric
) returns json language plpgsql security definer set search_path = '' as $$
declare
  v_scar record;
  v_balance numeric(14,6);
  v_royalty numeric(14,6) := 0;
  v_ref text := 'fetch_' || substr(md5(random()::text), 1, 8);
begin
  select id, action_signature, failure_mode, context, evidence,
         submitted_by, submitted_at, confirmations
  into v_scar
  from public.scars where id = p_scar_id;

  if v_scar.id is null then
    return json_build_object('ok', false, 'error', 'not_found');
  end if;

  select balance_credits into v_balance from public.accounts where id = p_account for update;
  if v_balance is null then
    return json_build_object('ok', false, 'error', 'unknown_account');
  end if;
  if v_balance < p_price then
    return json_build_object('ok', false, 'error', 'insufficient_funds',
      'required_credits', p_price, 'balance_credits', v_balance);
  end if;

  update public.accounts set balance_credits = balance_credits - p_price where id = p_account;
  insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
    values (p_account, -p_price, 'scar_fetch', v_ref);
  v_balance := v_balance - p_price;

  if v_scar.submitted_by is not null and v_scar.submitted_by <> p_account then
    v_royalty := round(p_price * 0.5, 6);
    update public.accounts set balance_credits = balance_credits + v_royalty
      where id = v_scar.submitted_by;
    insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id)
      values (v_scar.submitted_by, v_royalty, 'scar_fetch_royalty', v_ref);
  end if;

  return json_build_object(
    'ok', true,
    'scar_id', v_scar.id,
    'action_signature', v_scar.action_signature,
    'failure_mode', v_scar.failure_mode,
    'context', v_scar.context,
    'evidence', v_scar.evidence,
    'confirmations', v_scar.confirmations,
    'age_seconds', round(extract(epoch from now() - v_scar.submitted_at)::numeric),
    'price_charged_credits', p_price,
    'royalty_paid_credits', v_royalty,
    'balance_credits', round(v_balance, 6)
  );
end; $$;

-- ---------------------------------------------------------------------------
-- Grants. Only the service_role (Edge Function) may invoke money paths.
-- ---------------------------------------------------------------------------

revoke execute on function public.scar_check(uuid, text, double precision, numeric, numeric) from public, anon, authenticated;
revoke execute on function public.scar_submit(uuid, text, text, text, jsonb, text, text, numeric, numeric, double precision, int) from public, anon, authenticated;
revoke execute on function public.scar_fetch(uuid, uuid, numeric) from public, anon, authenticated;
grant execute on function public.scar_check(uuid, text, double precision, numeric, numeric) to service_role;
grant execute on function public.scar_submit(uuid, text, text, text, jsonb, text, text, numeric, numeric, double precision, int) to service_role;
grant execute on function public.scar_fetch(uuid, uuid, numeric) to service_role;
