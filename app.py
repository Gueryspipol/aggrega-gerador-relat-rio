"""Painel de Demandas (Trello) - SLA, pendências, operadoras e gerente.
Rodar:  streamlit run app.py
Segredos em .streamlit/secrets.toml: TRELLO_KEY, TRELLO_TOKEN, BOARD_ID, APP_PASSWORD
"""
import io
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import holidays
import numpy as np
import pandas as pd
import requests
import streamlit as st

# ---------------- CONFIGURAÇÃO (ajuste aqui) ----------------
FASES = {  # fase: (lista em que começa, lista em que é entregue)
    "Matriz": ("Solicitação de Matriz", "Devolução de Matriz"),
    "Estudo": ("Solicitação de estudo", "Devolução de estudo"),
}
LISTA_DIRETORIA = "Aprovação de Matriz Diretoria"
ETIQUETA_GERENTE = "Check Gerencial"
LISTA_MERCADO = "Análise de Mercado"  # usada só se o cartão não tiver o checklist "dd/mm - Direcionado ao mercado"
DATA_INICIAL = date(2026, 11, 4)  # só entram cartões criados a partir desta data (fase de teste)
ORDEM = {  # ordem padrão das colunas em cada aba do Excel (colunas fora da lista vão para o fim)
    "Cartões": ["Cartão", "Lista", "Responsáveis", "Etiquetas", "Atual", "Valor Atual", "Vidas", "Consultora", "CNPJ",
                "Gestor", "Oper Ofer", "Valor Ofer", "Porte", "SLA", "Data de Início", "Data de Entrega",
                "Data de Direcionamento", "Na lista desde", "Dias úteis na lista", "Última movimentação",
                "Data da última movimentação", "Última movimentação por", "Gerente", "Arquivado", "Link"],
    "Fases": ["Cartão", "Fase", "Responsáveis", "Lista", "Início", "Prazo", "Entrega", "SLA", "Dias úteis",
              "Entregue", "No prazo", "Vencida"],
    "Operadoras": ["Cartão", "Operadora", "Envio", "Resposta", "Dias úteis", "Pendente", "Responsáveis"],
    "Pendências": ["Cartão", "Fase", "Responsáveis", "Lista", "Causa", "Início", "Prazo", "SLA", "Dias úteis"],
}
JANELA_DIAS = 60  # a "última movimentação" é buscada só nos últimos X dias do histórico (bem mais rápido)
CAMPOS_DESC = ["Atual", "Valor Atual", "Vidas", "Consultora", "CNPJ", "Gestor", "Oper Ofer", "Valor Ofer"]
PORTES = [(2, 29, "PME", 2), (30, 99, "PME PLUS", 2), (100, 499, "MIDDLE", 10),
          (500, 999, "MIDDLE PLUS", 10), (1000, 10**9, "CORPORATE", 10)]  # (de, até, porte, SLA em dias úteis)
UF_FERIADOS = None  # ex.: "SP" para somar feriados estaduais aos nacionais
TZ = ZoneInfo("America/Sao_Paulo")
# ------------------------------------------------------------

FER = np.array(sorted(holidays.Brazil(subdiv=UF_FERIADOS, years=range(2024, 2031)).keys()), dtype="datetime64[D]")
RE_ITEM = re.compile(r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*[-–—]\s*(.+?)\s*$")
RE_PEND = re.compile(r"^\s*data de recebimento\s*[-–—]\s*(.+?)\s*$", re.I)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().casefold()


def dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def du(a, b):  # dias úteis entre duas datas
    a, b = (x.date() if isinstance(x, datetime) else x for x in (a, b))
    return int(np.busday_count(a, b, holidays=FER))


def mk(dia, mes, ano, ref, piso):  # monta a data de um "dd/mm" sem ano
    try:
        if ano:
            a = int(ano)
            return date(a + 2000 if a < 100 else a, int(mes), int(dia))
        c = date(ref.year, int(mes), int(dia))
        return c if c >= ref - timedelta(days=piso) else date(ref.year + 1, int(mes), int(dia))
    except ValueError:
        return None


def vidas_porte(desc):
    m = re.search(r"vidas[ \t\*]*:[ \t\*]*([\d\.]+)", desc or "", re.I)
    if not m or not m.group(1).strip("."):
        return None, None, None
    v = int(m.group(1).strip(".").replace(".", ""))
    for lo, hi, nome, sla in PORTES:
        if lo <= v <= hi:
            return v, nome, sla
    return v, None, None


def campos_desc(desc):  # linhas "- Atual: valor" -> {"Atual": "valor", ...}
    out, chaves = dict.fromkeys(CAMPOS_DESC, ""), {norm(k): k for k in CAMPOS_DESC}
    for linha in (desc or "").splitlines():
        rot, sep, val = re.sub(r"^[\s\-\*•]+", "", linha).replace("**", "").partition(":")
        if sep and norm(rot) in chaves:
            out[chaves[norm(rot)]] = val.strip()
    return out


def direcionamento(c, criado, entradas):  # data do checklist "dd/mm - Direcionado ao mercado"
    for ck in c["checklists"]:
        m = RE_ITEM.match(ck["name"])
        if m and "direcionado ao mercado" in norm(ck["name"]):
            d = mk(*m.group(1, 2, 3), criado.date(), 60)
            if d:
                return d
    return next((t.date() for t, n in entradas if n == norm(LISTA_MERCADO)), None)  # plano B: entrada na lista


ACOES = {"createCard": "Criou o cartão", "copyCard": "Copiou o cartão", "addChecklistToCard": "Adicionou checklist",
         "removeChecklistFromCard": "Removeu checklist", "createCheckItem": "Criou item", "updateCheckItem": "Editou item",
         "deleteCheckItem": "Excluiu item", "addMemberToCard": "Adicionou membro", "removeMemberFromCard": "Removeu membro",
         "addLabelToCard": "Adicionou etiqueta", "removeLabelFromCard": "Removeu etiqueta",
         "addAttachmentToCard": "Adicionou anexo", "deleteAttachmentFromCard": "Removeu anexo"}
CAMPOS_CARD = {"desc": "Editou a descrição", "due": "Alterou a data de entrega", "start": "Alterou a data de início",
               "name": "Renomeou o cartão", "closed": "Arquivou/desarquivou o cartão",
               "dueComplete": "Marcou/desmarcou a entrega como concluída"}


def descrever(a):  # texto curto da ação do histórico (Actions)
    d, t = a["data"], a["type"]
    if t == "updateCard":
        if d.get("listAfter"):
            return f'Moveu de "{d["listBefore"]["name"]}" para "{d["listAfter"]["name"]}"'
        return next((txt for k, txt in CAMPOS_CARD.items() if k in d.get("old", {})), "Atualizou o cartão")
    if t == "commentCard":
        return "Comentou: " + d.get("text", "")[:150]
    base = ACOES.get(t, t)
    if t == "updateCheckItemStateOnCard":
        base = "Marcou item" if d["checkItem"].get("state") == "complete" else "Desmarcou item"
    det = next((d[k]["name"] for k in ("checkItem", "checklist", "label", "attachment") if d.get(k, {}).get("name")), "")
    return f'{base}: "{det}"' if det else base


def api(path, **p):
    r = requests.get(f"https://api.trello.com/1{path}", timeout=60,
                     params={"key": st.secrets["TRELLO_KEY"], "token": st.secrets["TRELLO_TOKEN"], **p})
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300, show_spinner="Lendo o Trello…")
def carregar():
    b = st.secrets["BOARD_ID"]
    listas = {l["id"]: l["name"] for l in api(f"/boards/{b}/lists", filter="all")}
    cards = api(f"/boards/{b}/cards", filter="all", members="true", member_fields="fullName", checklists="all")

    def paginar(max_paginas, **p):  # histórico paginado, com limite de páginas para nunca ficar preso
        out, antes = [], None
        for _ in range(max_paginas):
            q = dict(p, limit=1000)
            if antes:
                q["before"] = antes
            lote = api(f"/boards/{b}/actions", **q)
            out += lote
            if len(lote) < 1000:
                break
            antes = lote[-1]["date"]
        return out

    moves = paginar(30, filter="updateCard:idList,createCard,copyCard")  # entradas nas listas (lógica de SLA)
    desde = (datetime.now(timezone.utc) - timedelta(days=JANELA_DIAS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recentes = paginar(6, filter="all", since=desde)  # tudo o que aconteceu na janela (última movimentação)
    return listas, cards, moves, recentes


def montar(prazo_trello, hoje):
    listas, cards, moves, recentes = carregar()
    ev, can, ult = {}, {}, {}
    for a in moves:  # entradas em cada lista, por cartão
        d = a["data"]
        nome = (d.get("listAfter") or d.get("list") or {}).get("name")
        cid = d.get("card", {}).get("id")
        if nome and cid:
            ev.setdefault(cid, []).append((dt(a["date"]), norm(nome)))
    for a in recentes:  # da mais nova para a mais antiga: a primeira de cada cartão é a última movimentação
        cid = a["data"].get("card", {}).get("id")
        if cid:
            ult.setdefault(cid, a)
    C, F, O = [], [], []
    for c in cards:
        resp = [m["fullName"] for m in c["members"]] or ["Sem responsável"]
        criado = datetime.fromtimestamp(int(c["id"][:8], 16), TZ)
        if criado.date() < DATA_INICIAL:
            continue
        e, atual = sorted(ev.get(c["id"], [])), listas.get(c["idList"], "?")
        desde = ([t for t, n in e if n == norm(atual)] or [criado])[-1]
        vidas, porte, sla = vidas_porte(c["desc"])
        due = dt(c["due"]).date() if c["due"] else None
        inicio = dt(c["start"]).date() if c.get("start") else None
        ua = ult.get(c["id"])
        C.append({"id": c["id"], "Cartão": c["name"], "Lista": atual, "Resp": resp,
                  "Etiquetas": ", ".join(l["name"] or l["color"] or "" for l in c["labels"]),
                  **campos_desc(c["desc"]),  # Atual, Valor Atual, Vidas, Consultora, CNPJ, Gestor, Oper Ofer, Valor Ofer
                  "Vidas": vidas, "Porte": porte, "SLA": sla,  # Vidas numérico (usado no cálculo do porte)
                  "Data de Início": inicio, "Data de Entrega": due,
                  "Data de Direcionamento": direcionamento(c, criado, e),
                  "Última movimentação": descrever(ua) if ua else f"Sem registro nos últimos {JANELA_DIAS} dias",
                  "Data da última movimentação": dt(ua["date"] if ua else c["dateLastActivity"]).replace(tzinfo=None)
                  if (ua or c.get("dateLastActivity")) else None,
                  "Última movimentação por": (ua.get("memberCreator") or {}).get("fullName", "") if ua else "",
                  "Gerente": any(norm(l["name"]) == norm(ETIQUETA_GERENTE) for l in c["labels"]),
                  "Arquivado": c["closed"], "Desde": desde.replace(tzinfo=None), "Link": c["shortUrl"]})
        for fase, (ini, fim) in FASES.items():
            t0 = next((t for t, n in e if n == norm(ini)), criado if (norm(atual) == norm(ini) and not e) else None)
            if not t0:
                continue
            t1 = next((t for t, n in e if n == norm(fim) and t >= t0), None)
            prazo = due if (prazo_trello and due) else (
                pd.Timestamp(np.busday_offset(t0.date(), sla, roll="forward", holidays=FER)).date() if sla else None)
            F.append(dict(id=c["id"], Cartão=c["name"], Fase=fase, Resp=resp, Lista=atual, Início=t0.date(),
                          Entrega=t1.date() if t1 else None, SLA=sla, Prazo=prazo, Entregue=bool(t1),
                          DiasUteis=du(t0, t1 or hoje),
                          NoPrazo=(t1.date() <= prazo) if (t1 and prazo) else None,
                          Vencida=bool(not t1 and not c["closed"] and prazo and hoje > prazo)))
        for ck in c["checklists"]:  # checklist "dd/mm - ..." = data de envio às operadoras
            m0 = RE_ITEM.match(ck["name"])
            envio = mk(*m0.group(1, 2, 3), criado.date(), 60) if m0 else None
            if not envio:
                continue
            for it in ck["checkItems"]:
                p = RE_PEND.match(it["name"])
                r = None if p else RE_ITEM.match(it["name"])
                if not (p or r):
                    continue
                op = (p or r).group(1 if p else 4)
                resp_dt = mk(*r.group(1, 2, 3), envio, 0) if r else None
                ok = bool(resp_dt and (resp_dt - envio).days <= 120)
                O.append(dict(id=c["id"], Cartão=c["name"], Operadora=can.setdefault(norm(op), op.strip()),
                              Envio=envio, Resposta=resp_dt if ok else None,
                              DiasUteis=du(envio, resp_dt) if ok else None, Pendente=bool(p), Resp=resp))
    cols = ["id", "Cartão", "Operadora", "Envio", "Resposta", "DiasUteis", "Pendente", "Resp"]
    return pd.DataFrame(C), pd.DataFrame(F), pd.DataFrame(O, columns=cols)


def vis(df, ordem=()):  # versão amigável para tela/Excel, com as colunas na ordem padrão
    d = df.drop(columns=["id"], errors="ignore").copy()
    if "Resp" in d:
        d["Resp"] = d.Resp.map(", ".join)
    d = d.rename(columns={"Resp": "Responsáveis", "DiasUteis": "Dias úteis", "NoPrazo": "No prazo", "Desde": "Na lista desde"})
    return d[[c for c in ordem if c in d] + [c for c in d if c not in ordem]]


# ---------------- TELA ----------------
st.set_page_config(page_title="Painel de Demandas", layout="wide")
st.markdown("""<style>.stApp{background:#f5f3ee}.k{background:#fff;border:1px solid #e3e0d8;border-radius:12px;
padding:16px;min-height:130px}.k b{font-size:32px;color:#2b2b2b}.k span{color:#d9c59f;font-weight:800;font-size:26px;
float:right}.k small{display:block;color:#2b2b2b;font-weight:700;margin-top:10px;font-size:15px}</style>""",
            unsafe_allow_html=True)
if st.secrets.get("APP_PASSWORD") and st.sidebar.text_input("Senha", type="password") != st.secrets["APP_PASSWORD"]:
    st.info("Digite a senha na barra lateral.")
    st.stop()

hoje = datetime.now(TZ).date()
with st.sidebar:
    ref = st.radio("Prazo de referência", ["SLA calculado", "Data de entrega do Trello"])
    per = st.date_input("Abertura da fase entre", (DATA_INICIAL, hoje), min_value=DATA_INICIAL)
    if st.button("Atualizar agora"):
        st.cache_data.clear()
        st.rerun()
try:
    cartoes, fases, ops = montar(ref != "SLA calculado", hoje)
except Exception as ex:
    st.error(f"Não consegui ler o Trello: {ex}")
    st.stop()
if fases.empty:
    st.warning("Nenhum cartão encontrado nas listas de início configuradas (veja FASES no app.py).")
    st.stop()
todos = sorted({r for l in cartoes.Resp for r in l})
sel = st.sidebar.multiselect("Analistas", todos, default=todos)
tem = lambda l: bool(set(l) & set(sel))
if len(per) == 2:
    fases = fases[(fases.Início >= per[0]) & (fases.Início <= per[1])]
fases, cartoes = fases[fases.Resp.map(tem)], cartoes[cartoes.Resp.map(tem)]
ops = ops[ops.Resp.map(tem) & ops.id.isin(fases.id)]

# Indicador 01
ent = fases[fases.Entregue & fases.NoPrazo.notna()].copy()
ent["NoPrazo"] = ent.NoPrazo.astype(float)
pct1 = ent.NoPrazo.mean() * 100 if len(ent) else None
# Indicador 02
venc = fases[fases.Vencida].copy()


def causa(r):
    pend = ops[(ops.id == r.id) & ops.Pendente].Operadora.unique()
    if len(pend):
        return "Aguardando operadora: " + ", ".join(pend)
    return "Aguardando diretoria" if norm(r.Lista) == norm(LISTA_DIRETORIA) else "Análise interna"


venc["Causa"] = [causa(r) for r in venc.itertuples()]
venc["Tipo"] = venc.Causa.str.split(":").str[0] if len(venc) else []
# Indicador 03
resp_ops = ops.dropna(subset=["DiasUteis"])
media3 = resp_ops.DiasUteis.mean() if len(resp_ops) else None
# Indicador 04
conc = cartoes[cartoes.id.isin(fases[fases.Entregue].id)]
pct4 = (~conc.Gerente).mean() * 100 if len(conc) else None

st.title("Painel de Demandas")
fmt = lambda v, s: "—" if v is None or pd.isna(v) else s.format(v)
k = st.columns(4)
for col, (n, tit, val) in zip(k, [
        (1, "% de projetos entregues no SLA", fmt(pct1, "{:.0f}%")),
        (2, "Pendências vencidas por responsável e causa", str(len(venc))),
        (3, "Tempo médio de resposta das operadoras", fmt(media3, "{:.1f} dias úteis")),
        (4, "% de negociações resolvidas sem o gerente", fmt(pct4, "{:.0f}%"))]):
    col.markdown(f'<div class="k"><span>0{n}</span><b>{val}</b><small>{tit}</small></div>', unsafe_allow_html=True)

t1, t2, t3, t4, t5 = st.tabs(["01 SLA", "02 Pendências", "03 Operadoras", "04 Gerente", "Fluxo"])
with t1:
    if len(ent):
        g = ent.explode("Resp").groupby(["Fase", "Resp"]).agg(Entregas=("NoPrazo", "size"), NoPrazo=("NoPrazo", "mean"))
        g["NoPrazo"] = (g.NoPrazo * 100).round(0)
        st.dataframe(g.rename(columns={"NoPrazo": "% no prazo"}), use_container_width=True)
        st.dataframe(vis(ent, ORDEM["Fases"]), use_container_width=True, hide_index=True)
    else:
        st.info("Ainda não há entregas com porte (campo Vidas) preenchido neste período.")
    sem = cartoes[cartoes.Vidas.isna()]
    if len(sem):
        st.caption(f"{len(sem)} cartão(ões) sem 'Vidas' preenchido — ficam fora do cálculo de SLA.")
with t2:
    if len(venc):
        st.bar_chart(venc.explode("Resp").groupby(["Resp", "Tipo"]).size().unstack(fill_value=0))
        st.dataframe(vis(venc.drop(columns=["Tipo"]), ORDEM["Pendências"]), use_container_width=True, hide_index=True)
    else:
        st.success("Nenhuma pendência vencida.")
with t3:
    if len(ops):
        t = ops.groupby("Operadora").agg(Respondidas=("DiasUteis", "count"), Média=("DiasUteis", "mean"),
                                         Pendentes=("Pendente", "sum")).round(1).sort_values("Média")
        st.dataframe(t.rename(columns={"Média": "Média (dias úteis)"}), use_container_width=True)
        st.dataframe(vis(ops, ORDEM["Operadoras"]), use_container_width=True, hide_index=True)
    else:
        st.info("Nenhum checklist de operadoras no padrão 'dd/mm' encontrado.")
with t4:
    if len(conc):
        g = conc.explode("Resp").groupby("Resp").agg(Cartões=("Gerente", "size"), ComGerente=("Gerente", "sum"))
        st.dataframe(g, use_container_width=True)
        st.dataframe(vis(conc, ORDEM["Cartões"]), use_container_width=True, hide_index=True)
    else:
        st.info("Ainda não há cartões entregues no período.")
with t5:
    ab = cartoes[~cartoes.Arquivado].copy()
    ab["Dias úteis na lista"] = [du(t, hoje) for t in ab.Desde]
    st.bar_chart(ab.Lista.value_counts())
    st.dataframe(vis(ab, ORDEM["Cartões"]), use_container_width=True, hide_index=True)

buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as w:
    for nome, d in {"Cartões": cartoes, "Fases": fases, "Operadoras": ops, "Pendências": venc}.items():
        vis(d, ORDEM[nome]).to_excel(w, sheet_name=nome, index=False)
        ws = w.sheets[nome]
        ws.freeze_panes = "A2"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col[:300]) + 2, 45)
            for c in col[1:]:
                if isinstance(c.value, datetime):
                    c.number_format = "DD/MM/YYYY HH:MM"
                elif isinstance(c.value, date):
                    c.number_format = "DD/MM/YYYY"
st.sidebar.download_button("Baixar Excel", buf.getvalue(), "relatorio_demandas.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
