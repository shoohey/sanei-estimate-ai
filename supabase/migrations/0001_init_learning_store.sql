-- サンエー見積AI 学習データ永続化スキーマ (v1)
-- 設計: RLS有効・ポリシー無し = service_role キーのみアクセス可（anon は読み書き不可）
-- 冪等: 再実行安全

-- 学習ストア等のKVドキュメント（従来のJSONファイル1個 = 1行）
create table if not exists app_storage (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);
alter table app_storage enable row level security;

-- 見積履歴（学習の材料）
create table if not exists estimate_history (
  id bigint generated always as identity primary key,
  saved_at timestamptz not null default now(),
  estimate_id text not null default '',
  client_name text not null default '',
  project_name text not null default '',
  total_with_tax bigint not null default 0,
  payload jsonb not null
);
alter table estimate_history enable row level security;

-- 図面履歴（学習の材料）
create table if not exists drawing_history (
  id bigint generated always as identity primary key,
  saved_at timestamptz not null default now(),
  customer_name text not null default '',
  drawing_type text not null default '',
  total_panels int not null default 0,
  total_kw numeric not null default 0,
  payload jsonb not null
);
alter table drawing_history enable row level security;
