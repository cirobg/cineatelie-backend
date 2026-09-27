-- =====================================================================================
-- Cine Ateliê — platform reference data (tenant-agnostic)
-- Extracted verbatim from schema-20260920-cineatelie.sql section 17.
-- Safe to re-run in any environment: every INSERT is guarded by ON CONFLICT DO NOTHING.
-- Already applied once as part of db/baseline/00001_baseline.sql (revision 0001); this
-- copy exists so reference data can be reseeded independently of a schema migration —
-- e.g. in CI, or when a new environment is provisioned from an already-migrated dump.
-- =====================================================================================

SET search_path = cineatelie;
BEGIN;

INSERT INTO roles (code, label_pt_br, description, sort_order) VALUES
  ('owner',      'Proprietária', 'Full access to the workspace, billing and team.',  10),
  ('finance',    'Financeiro',   'Cash flow, quotes, clients and contracts.',        20),
  ('seamstress', 'Costureira',   'Production board, stock, services and suppliers.', 30),
  ('front_desk', 'Atendimento',  'Quotes, clients, services and ready-to-wear.',     40)
ON CONFLICT (code) DO NOTHING;

INSERT INTO permissions (code, resource, action, description) VALUES
  ('dashboard:read','dashboard','read','View the overview dashboard'),
  ('quotes:read','quotes','read','List and open quotes'),
  ('quotes:write','quotes','write','Create and edit quotes'),
  ('quotes:approve','quotes','manage','Approve or refuse a quote'),
  ('clients:read','clients','read','List and open clients'),
  ('clients:write','clients','write','Create and edit clients and measurement sheets'),
  ('clients:delete','clients','delete','Remove a client (LGPD erasure)'),
  ('services:read','services','read','View the service catalogue'),
  ('services:write','services','write','Maintain the service catalogue'),
  ('stock:read','stock','read','View stock and inputs'),
  ('stock:write','stock','write','Register stock entries and adjustments'),
  ('suppliers:read','suppliers','read','View suppliers'),
  ('suppliers:write','suppliers','write','Maintain suppliers'),
  ('rtw:read','ready_to_wear','read','View ready-to-wear pieces'),
  ('rtw:write','ready_to_wear','write','Maintain ready-to-wear pieces'),
  ('orders:read','service_orders','read','View the production board'),
  ('orders:write','service_orders','write','Move cards, edit orders, attach files'),
  ('agenda:read','agenda','read','View the calendar'),
  ('agenda:write','agenda','write','Create and edit appointments'),
  ('finance:read','finance','read','View cash flow'),
  ('finance:write','finance','write','Create and settle cash-flow entries'),
  ('contracts:read','contracts','read','View and print contracts and receipts'),
  ('contracts:write','contracts','write','Edit contract templates'),
  ('settings:read','settings','read','View workspace settings'),
  ('settings:write','settings','write','Change workspace settings and card fees'),
  ('team:manage','team','manage','Invite, change role and revoke members'),
  ('billing:manage','billing','manage','Manage the subscription and invoices')
ON CONFLICT (code) DO NOTHING;

-- owner = every permission
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'owner', code FROM permissions
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_code, permission_code) VALUES
  ('finance','dashboard:read'),('finance','quotes:read'),('finance','quotes:write'),
  ('finance','clients:read'),('finance','finance:read'),('finance','finance:write'),
  ('finance','contracts:read'),
  ('seamstress','dashboard:read'),('seamstress','orders:read'),('seamstress','orders:write'),
  ('seamstress','stock:read'),('seamstress','stock:write'),('seamstress','services:read'),
  ('seamstress','rtw:read'),('seamstress','suppliers:read'),('seamstress','agenda:read'),
  ('front_desk','dashboard:read'),('front_desk','quotes:read'),('front_desk','quotes:write'),
  ('front_desk','clients:read'),('front_desk','clients:write'),('front_desk','services:read'),
  ('front_desk','rtw:read'),('front_desk','agenda:read'),('front_desk','agenda:write')
ON CONFLICT DO NOTHING;

INSERT INTO features (key, label_pt_br, description) VALUES
  ('dashboard','Visão geral','Overview dashboard'),
  ('quotes','Orçamentos','Quote editor and pipeline'),
  ('clients','Clientes','Client registry and measurement sheet'),
  ('services','Serviços','Service catalogue'),
  ('stock','Estoque e insumos','Materials, movements and cost history'),
  ('suppliers','Fornecedores','Supplier registry'),
  ('ready_to_wear','Pronta entrega','Ready-to-wear sale and rental'),
  ('orders','Ordens de serviço','Production Kanban and finished-order history'),
  ('agenda','Agenda','Calendar and appointments'),
  ('finance','Fluxo de caixa','Receivables and payables'),
  ('contracts','Contratos','Contract and receipt templates'),
  ('team','Equipe','Multi-user access with roles'),
  ('attachments','Anexos','File attachments on orders'),
  ('courses','Cursos','Roadmap: training content'),
  ('whatsapp','WhatsApp','Roadmap: WhatsApp messaging'),
  ('email_relay','E-mail','Roadmap: transactional e-mail relay'),
  ('open_finance','Open Finance','Roadmap: bank feed integration'),
  ('ai_assistant','Assistente IA','Roadmap: AI assistants')
ON CONFLICT (key) DO NOTHING;

-- =====================================================================================
-- Plans — ONE paid plan at launch (OI-01, decided 2026-09-25).
-- =====================================================================================
-- A single "Starter" plan with every shipped feature enabled, plus a 7-day trial of the
-- same thing. A second tier was considered and dropped: its only differentiators would
-- have been team seats and attachments, which is too thin to price separately and tends
-- to confuse buyers rather than upsell them.
--
-- Adding a tier later is two INSERTs (a `plans` row and its `plan_features` rows) — no
-- schema change, no deploy. It is NOT seeded here as a dormant placeholder, because a
-- visible-but-unused plan invites exactly the confusion the decision avoids.
--
-- price_amount is 0 on purpose: the price is still undecided. Changing it later is one
-- UPDATE, and `subscriptions.price_amount` snapshots what each tenant actually agreed to
-- pay, so repricing never rewrites history and existing subscribers are grandfathered.
-- =====================================================================================
INSERT INTO plans (code, label_pt_br, description, price_amount, billing_period, trial_days, sort_order) VALUES
  ('trial',  'Avaliação', 'Avaliação de 7 dias, com todos os recursos do plano Starter.', 0, 'annual', 7,  5),
  ('starter','Starter',   'Gestão completa do ateliê.',                                   0, 'annual', 0, 10)
ON CONFLICT (code) DO NOTHING;

-- Every shipped feature is enabled on both plans. Only the roadmap features — which have
-- no implementation behind them yet — are off; they are seeded so the gate exists the day
-- the feature lands (ADR-006).
INSERT INTO plan_features (plan_code, feature_key, is_enabled, limit_value)
SELECT p.code,
       f.key,
       f.key NOT IN ('courses','whatsapp','email_relay','open_finance','ai_assistant'),
       -- The one place where "all features" carries a number. 5 seats is generous for a
       -- micro-atelier while preserving a future pricing lever. RAISING it later is free;
       -- LOWERING it would take something away from existing tenants and would need
       -- grandfathering via tenant_feature_overrides. See OI-01 note (d).
       CASE WHEN f.key = 'team' THEN 5 END
FROM plans p CROSS JOIN features f
ON CONFLICT DO NOTHING;

-- =====================================================================================
-- Quota catalogue and the Starter ceilings.
-- =====================================================================================
-- The numbers below are the working assumption behind the Starter price (OI-13). They are
-- sized against the Supabase free tier — 1 GB file storage, 500 MB database, 5 GB egress
-- per month — so that one tenant on Starter cannot exhaust it alone. Raise them per tenant
-- with tenant_quota_overrides, or per plan when a paid tier exists; raising is always safe,
-- lowering takes something away (see architecture, "Changing packaging later").
INSERT INTO quotas (key, label_pt_br, unit, description) VALUES
  ('attachments_per_item', 'Anexos por item', 'count',
   'Attachments on ONE service order or ONE ready-to-wear piece. The number the user actually feels.'),
  ('attachments_total',    'Total de anexos', 'count',
   'Attachments across the whole workspace. The marketed ceiling and the pricing lever.'),
  ('max_attachment_mb',    'Tamanho máximo por arquivo', 'megabytes',
   'Per-file ceiling. With attachments_total, this is what bounds worst-case storage: total x max.'),
  ('storage_mb_total',     'Espaço total de arquivos', 'megabytes',
   'Safety ceiling only, NOT the marketed quota. NULL on every plan because attachments_total x '
   'max_attachment_mb already bounds the cost. Set it per tenant only to stop a pathological case.')
ON CONFLICT (key) DO NOTHING;

-- Tier ceilings (owner decision, 2026-09-26). Counts, not megabytes: a count is what the
-- user understands and what the plan is sold on, while max_attachment_mb quietly bounds the
-- cost — worst case is attachments_total x max_attachment_mb, which is predictable.
--
--   trial   :  1 per item,   10 total  ->  worst case   50 MB
--   starter :  3 per item,  300 total  ->  worst case  1.5 GB, realistic ~600 MB at 2 MB/file
--   pro     : 10 per item, 1500 total  ->  priced to cover its own storage
--
-- 'pro' is NOT seeded — there is no Pro plan yet (OI-01). Its numbers are recorded here so the
-- shape of the upgrade is decided even though the plan is not.
INSERT INTO plan_quotas (plan_code, quota_key, limit_value) VALUES
  ('trial',   'attachments_per_item',     1),
  ('trial',   'attachments_total',       10),
  ('trial',   'max_attachment_mb',        5),
  ('trial',   'storage_mb_total',      NULL),
  ('starter', 'attachments_per_item',     3),
  ('starter', 'attachments_total',      300),
  ('starter', 'max_attachment_mb',        5),
  ('starter', 'storage_mb_total',      NULL)
ON CONFLICT DO NOTHING;

-- =====================================================================================
-- The 42 fields of page 1 of the paper measurement sheet.
-- =====================================================================================
-- CALIBRATED 2026-09-24 (build task BT-01, closed).
--
-- Both `top_pct` and `label_pt_br` are taken VERBATIM from the design prototype
-- `01-product/design_handoff_cine_atelie/Ficha de Medidas.dc.html`, which carries the
-- designer's own tuned values in each field's `top:` CSS property. They were then
-- cross-checked against the croqui images themselves:
--
--     landmark in croqui-frente.png   value    field
--     shoulder line      ~15-17%      15.3     ombroOmbroF
--     bust apex          ~24-26%      23.8     lBusto
--     waist              ~33-34%      33.5     lCintura
--     hip                ~44-45%      45.3     lQuadril
--     knee               ~65%         65.2     lJoelho
--     ankle              ~89-92%      89.2     lTornozelo
--     gluteal fold (costas) ~47%      47.0     altGancho
--
-- `top_pct` is the vertical CENTRE of the field row, not its top edge: the prototype
-- applies `transform: translateY(-50%)` to every row. A renderer that treats it as the
-- top edge will sit every field ~12 px low.
--
-- Labels are the prototype's pt-BR copy exactly, including its own capitalisation
-- ("Altura da manga", "Cava a cava Frente"). The handoff declares copy final; do not
-- normalise it.
-- =====================================================================================
INSERT INTO measurement_fields
  (key, panel, column_side, sort_order, label_pt_br, value_type, unit, placeholder, top_pct) VALUES
  -- Panel 1 — Frente · larguras e comprimentos (left column: label before input)
  ('ombroOmbroF','front_widths','left', 10,'Ombro a Ombro Frente','decimal','cm','cm',15.300),
  ('lBiceps','front_widths','left', 20,'Largura do Bíceps','decimal','cm','cm',24.300),
  ('altCotovelo','front_widths','left', 30,'Altura do Cotovelo','decimal','cm','cm',31.300),
  ('lCotovelo','front_widths','left', 40,'Largura do Cotovelo','decimal','cm','cm',37.300),
  ('lPunho','front_widths','left', 50,'Largura do Punho','decimal','cm','cm',44.300),
  ('altManga','front_widths','left', 60,'Altura da manga','decimal','cm','cm',51.300),
  ('compSaia','front_widths','left', 70,'Comprimento da Saia','decimal','cm','cm',62.300),
  ('lTornozelo','front_widths','left', 80,'Largura do Tornozelo','decimal','cm','cm',89.200),
  ('compVestido','front_widths','left', 90,'Comprimento Total do Vestido','decimal','cm','cm',95.900),
  -- Panel 1 (right column: input before label)
  ('lPescoco','front_widths','right', 10,'Largura do Pescoço','decimal','cm','cm',12.300),
  ('cavaCavaF','front_widths','right', 20,'Cava a cava Frente','decimal','cm','cm',19.300),
  ('lBusto','front_widths','right', 30,'Largura do Busto','decimal','cm','cm',23.800),
  ('lAbaixoBusto','front_widths','right', 40,'Largura do Abaixo do Busto','decimal','cm','cm',27.500),
  ('lCintura','front_widths','right', 50,'Largura da Cintura','decimal','cm','cm',33.500),
  ('altQuadril','front_widths','right', 60,'Altura do Quadril','decimal','cm','cm',42.000),
  ('lQuadril','front_widths','right', 70,'Largura do Quadril','decimal','cm','cm',45.300),
  ('l10Quadril','front_widths','right', 80,'Largura de 10cm Abaixo do Quadril','decimal','cm','cm',48.700),
  ('l15Quadril','front_widths','right', 90,'Largura de 15cm Abaixo do Quadril','decimal','cm','cm',52.000),
  ('lCoxa','front_widths','right',100,'Largura da Coxa','decimal','cm','cm',61.200),
  ('lJoelho','front_widths','right',110,'Largura do Joelho','decimal','cm','cm',65.200),
  ('altJoelho','front_widths','right',120,'Altura do Joelho','decimal','cm','cm',68.700),
  -- Panel 2 — Frente · alturas (left)
  ('transversalF','front_heights','left', 10,'Transversal Frente','decimal','cm','cm',13.600),
  ('ombro','front_heights','left', 20,'Ombro','decimal','cm','cm',18.200),
  ('entresseio','front_heights','left', 30,'Entresseio','decimal','cm','cm',24.000),
  ('lBustoF','front_heights','left', 40,'Largura do Busto Frente','decimal','cm','cm',29.000),
  ('raioBusto','front_heights','left', 50,'Raio do Busto','decimal','cm','cm',34.000),
  -- Panel 2 (right)
  ('altFrente','front_heights','right', 10,'Altura do Corpo Frente','decimal','cm','cm',11.000),
  ('centroF','front_heights','right', 20,'Altura do Centro Frente','decimal','cm','cm',16.000),
  ('altCavaF','front_heights','right', 30,'Altura da Cava','decimal','cm','cm',21.000),
  ('altBusto','front_heights','right', 40,'Altura do Busto','decimal','cm','cm',26.000),
  ('altAbaixoBusto','front_heights','right', 50,'Altura do Abaixo do Busto','decimal','cm','cm',31.000),
  ('altLateral','front_heights','right', 60,'Altura da Lateral do Corpo','decimal','cm','cm',37.000),
  -- Panel 3 — Costas (left)
  ('ombroOmbroC','back','left', 10,'Ombro a Ombro Costas','decimal','cm','cm',17.100),
  ('cavaCavaC','back','left', 20,'Cava a Cava Costas','decimal','cm','cm',23.300),
  -- Panel 3 (right)
  ('transversalC','back','right', 10,'Transversal Costas','decimal','cm','cm',13.000),
  ('altCosta','back','right', 20,'Altura do Corpo Costas','decimal','cm','cm',18.000),
  ('centroC','back','right', 30,'Altura do Centro Costas','decimal','cm','cm',23.000),
  ('altCavaC','back','right', 40,'Altura da Cava Costas','decimal','cm','cm',27.600),
  ('curvaturaLombar','back','right', 50,'Curvatura da Lombar','text',NULL,'P, M, G',35.300),
  ('altGancho','back','right', 60,'Altura do Gancho','decimal','cm','cm',47.000),
  ('altLateralPerna','back','right', 70,'Altura da Lateral da Perna','decimal','cm','cm',51.700),
  ('altEntrepernas','back','right', 80,'Altura do Entrepernas','decimal','cm','cm',56.700)
ON CONFLICT (key) DO NOTHING;

COMMIT;
