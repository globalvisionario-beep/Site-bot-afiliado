#!/usr/bin/env python3
"""
Bot de ofertas (afiliados Shopee Brasil).

Uso:
  python3 bot/atualizar.py                      -> atualização diária (o que o GitHub Actions roda)
  python3 bot/atualizar.py diagnostico "texto"  -> testa uma busca na API (não altera o site)
  python3 bot/atualizar.py diagnostico 123456   -> testa a consulta de um itemId (não altera o site)

Variáveis de ambiente obrigatórias:
  SHOPEE_APP_ID, SHOPEE_APP_SECRET

Só usa a biblioteca padrão do Python (nada para instalar).
"""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"
RAIZ = Path(__file__).resolve().parent.parent
ARQ_PRODUTOS = RAIZ / "produtos.json"
ARQ_CONFIG = Path(__file__).resolve().parent / "config.json"
FUSO = timezone(timedelta(hours=-3))  # horário de Brasília

CAMPOS = (
    "itemId productName price priceMin priceMax priceDiscountRate "
    "sales ratingStar imageUrl productLink offerLink"
)
TEMPLATE_BUSCA = (
    "query { productOfferV2(keyword: %KW%, sortType: %ORD%, page: %PG%, limit: %LIM%) "
    "{ nodes { %CAMPOS% } pageInfo { hasNextPage } } }"
)
TEMPLATE_ID = "query { productOfferV2(itemId: %ID%, limit: 1) { nodes { %CAMPOS% } } }"
STOPWORDS = {"para", "com", "sem", "por", "uma", "uns", "das", "dos"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PADROES_ID = [re.compile(r"i\.(\d{5,})\.(\d{5,})"), re.compile(r"/product/(\d{5,})/(\d{5,})")]


class ErroAPI(Exception):
    pass


def log(*args):
    print(*args, flush=True)


# --------------------------------------------------------------------------
# Comunicação com a API
# --------------------------------------------------------------------------
def chamar_api(query, app_id, secret):
    """Envia uma consulta GraphQL assinada. Tenta 3 vezes em caso de falha de rede."""
    corpo = json.dumps({"query": query}, separators=(",", ":"), ensure_ascii=False)
    ultimo_erro = None
    for tentativa in range(3):
        ts = str(int(time.time()))
        assinatura = hashlib.sha256((app_id + ts + corpo + secret).encode("utf-8")).hexdigest()
        req = urllib.request.Request(
            ENDPOINT,
            data=corpo.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"SHA256 Credential={app_id}, Timestamp={ts}, Signature={assinatura}",
            },
        )
        dados = None
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                dados = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")
            try:
                dados = json.loads(texto)
            except ValueError:
                dados = None
            if not dados or "errors" not in dados:
                ultimo_erro = ErroAPI(f"HTTP {e.code}: {texto[:300]}")
                time.sleep(2 * (tentativa + 1))
                continue
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            ultimo_erro = ErroAPI(f"Falha de rede: {e}")
            time.sleep(2 * (tentativa + 1))
            continue

        if dados.get("errors"):
            raise ErroAPI(json.dumps(dados["errors"], ensure_ascii=False)[:600])
        time.sleep(0.4)  # respeita o limite de requisições
        return dados.get("data") or {}
    raise ultimo_erro or ErroAPI("Falha desconhecida")


# --------------------------------------------------------------------------
# Tratamento dos dados
# --------------------------------------------------------------------------
def para_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalizar(texto):
    texto = unicodedata.normalize("NFD", str(texto).lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", texto)


def extrair_ids(texto):
    """Acha (shopId, itemId) em um endereço da Shopee. Devolve None se não achar."""
    texto = urllib.parse.unquote(urllib.parse.unquote(texto or ""))
    for padrao in PADROES_ID:
        m = padrao.search(texto)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


class _SemRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolver_link(url, maximo=6):
    """Segue o link curto de afiliado e descobre o produto. Devolve (shopId, itemId) ou None."""
    ids = extrair_ids(url)
    if ids:
        return ids
    abridor = urllib.request.build_opener(_SemRedirect)
    atual = url
    for _ in range(maximo):
        req = urllib.request.Request(atual, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        destino, corpo = None, ""
        try:
            with abridor.open(req, timeout=20) as resp:
                destino = resp.headers.get("Location")
                corpo = resp.read(300000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            destino = e.headers.get("Location")
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None
        for texto in (destino or "", corpo):
            ids = extrair_ids(texto)
            if ids:
                return ids
        if not destino:
            return None
        atual = urllib.parse.urljoin(atual, destino)
    return None


def checar_estoque(shop_id, item_id):
    """True = esgotado, False = tem estoque, None = não deu para saber (a Shopee pode bloquear)."""
    if not shop_id:
        return None
    url = f"https://shopee.com.br/api/v4/item/get?itemid={int(item_id)}&shopid={int(shop_id)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json", "Referer": "https://shopee.com.br/",
                 "X-Requested-With": "XMLHttpRequest"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    item = (dados or {}).get("data") or {}
    item = item.get("item", item) if isinstance(item, dict) else {}
    estoque = item.get("normal_stock", item.get("stock"))
    if estoque is None:
        return None
    return int(para_float(estoque)) <= 0


def gerar_busca(nome, quantidade=3):
    """Cria a palavra-chave de busca a partir do nome (primeiras palavras úteis)."""
    palavras = []
    for w in normalizar(nome).split():
        if len(w) < 3 or w in STOPWORDS or any(c.isdigit() for c in w):
            continue
        palavras.append(w)
        if len(palavras) == quantidade:
            break
    return " ".join(palavras)


def normalizar_no(n):
    preco = para_float(n.get("price")) or para_float(n.get("priceMin"))
    desconto = int(para_float(n.get("priceDiscountRate")))
    if preco and 0 < desconto < 95:
        original = round(preco / (1 - desconto / 100), 2)
    else:
        original = preco
    return {
        "itemId": int(para_float(n.get("itemId"))),
        "nome": (n.get("productName") or "").strip(),
        "preco": round(preco, 2),
        "preco_original": original,
        "desconto": desconto,
        "vendas": int(para_float(n.get("sales"))),
        "nota": round(para_float(n.get("ratingStar")), 2),
        "imagem": n.get("imageUrl") or "",
        "link": n.get("offerLink") or "",
        "shopId": (extrair_ids(n.get("productLink")) or (None,))[0],
        "bruto": n,
    }


def buscar(busca, cfg, app_id, secret):
    """Busca por palavra-chave (relevância e mais vendidos) e junta os resultados."""
    vistos, itens = set(), []
    for ordenacao in (1, 2):  # 1 = relevância, 2 = vendas
        for pagina in range(1, int(cfg["paginas_busca"]) + 1):
            q = (
                TEMPLATE_BUSCA.replace("%KW%", json.dumps(busca, ensure_ascii=False))
                .replace("%ORD%", str(ordenacao))
                .replace("%PG%", str(pagina))
                .replace("%LIM%", str(int(cfg["itens_por_pagina"])))
                .replace("%CAMPOS%", CAMPOS)
            )
            bloco = chamar_api(q, app_id, secret).get("productOfferV2") or {}
            for n in bloco.get("nodes") or []:
                item = normalizar_no(n)
                if item["itemId"] and item["itemId"] not in vistos:
                    vistos.add(item["itemId"])
                    itens.append(item)
            if not (bloco.get("pageInfo") or {}).get("hasNextPage"):
                break
    return itens


def buscar_por_id(item_id, app_id, secret):
    q = TEMPLATE_ID.replace("%ID%", str(int(item_id))).replace("%CAMPOS%", CAMPOS)
    nos = (chamar_api(q, app_id, secret).get("productOfferV2") or {}).get("nodes") or []
    return normalizar_no(nos[0]) if nos else None


def palavras_obrigatorias(produto):
    base = produto.get("palavras_obrigatorias") or produto.get("busca", "").split()
    saida = []
    for palavra in base:
        for w in normalizar(palavra).split():
            if len(w) >= 3 and w not in STOPWORDS:
                saida.append(w[:5] if len(w) > 5 else w)  # raiz da palavra (eletrico/eletrica)
    return saida


def escolher(itens, produto, cfg, excluir_ids):
    """Escolhe o produto MAIS BARATO que cumpre todos os critérios."""
    obrigatorias = palavras_obrigatorias(produto)
    referencia = produto.get("preco_original") or produto.get("preco") or 0
    faixa = cfg.get("faixa_preco") or [0, 0]
    piso = para_float(produto.get("preco_minimo"))
    teto = para_float(produto.get("preco_maximo"))

    def ok(i, vendas_min):
        if i["itemId"] in excluir_ids or not i["link"] or i["preco"] <= 0:
            return False
        if i["nota"] < cfg["nota_minima"] or i["vendas"] < vendas_min:
            return False
        if i["desconto"] < cfg["desconto_minimo_percentual"]:
            return False
        nome = normalizar(i["nome"])
        if not all(p in nome for p in obrigatorias):
            return False
        if referencia and faixa[1]:
            if not (referencia * faixa[0] <= i["preco"] <= referencia * faixa[1]):
                return False
        if piso and i["preco"] < piso:
            return False
        if teto and i["preco"] > teto:
            return False
        return True

    for vendas_min in (cfg["vendas_preferidas"], cfg["vendas_minimas"]):
        candidatos = [i for i in itens if ok(i, vendas_min)]
        if candidatos:
            return min(candidatos, key=lambda i: (i["preco"], -i["vendas"]))
    return None


def aplicar(produto, item, cfg, agora, manter_link=False):
    produto.update(
        {
            "itemId": item["itemId"],
            "nome": item["nome"] or produto.get("nome", ""),
            "imagem": produto.get("imagem_propria") or item["imagem"] or produto.get("imagem", ""),
            "preco": item["preco"],
            "preco_original": item["preco_original"],
            "desconto": item["desconto"],
            "nota": item["nota"],
            "vendas": item["vendas"],
            "em_promocao": item["desconto"] >= cfg["desconto_minimo_percentual"],
            "verificado_em": agora,
        }
    )
    if item.get("shopId"):
        produto["shopId"] = item["shopId"]
    if item["link"] and not manter_link:
        produto["link"] = item["link"]


def preencher_por_link(p, cfg, app_id, secret, agora, ids_no_site):
    """Produto novo com só o link de afiliado: descobre o produto e preenche tudo."""
    codigo = p.get("codigo")
    ids = resolver_link(p["link"])
    if not ids:
        p["erro_link"] = ("Não consegui ler este link. Copie o endereço normal do produto "
                          "(termina com i.números.números) ou informe o itemId nas opções avançadas.")
        log(f"[{codigo}] não consegui descobrir o produto pelo link.")
        return
    shop_id, item_id = ids
    item = buscar_por_id(item_id, app_id, secret)
    if not item:
        p["erro_link"] = "Este produto não foi encontrado na API de afiliados da Shopee."
        log(f"[{codigo}] itemId {item_id} não encontrado na API.")
        return
    p.pop("erro_link", None)
    aplicar(p, item, cfg, agora, manter_link=True)
    p["shopId"] = item.get("shopId") or shop_id
    if not p.get("busca"):
        p["busca"] = gerar_busca(item["nome"], int(cfg.get("palavras_na_busca", 3)))
    ids_no_site.add(item_id)
    log(f"[{codigo}] preenchido pelo link: R$ {item['preco']:.2f} | {item['nome'][:50]} | busca: {p['busca']}")


# --------------------------------------------------------------------------
# Rotina principal
# --------------------------------------------------------------------------
def atualizar(cfg, app_id, secret):
    with open(ARQ_PRODUTOS, encoding="utf-8") as f:
        site = json.load(f)
    produtos = site.get("produtos", [])
    historico = site.setdefault("historico_trocas", [])
    agora = datetime.now(FUSO).isoformat(timespec="seconds")
    ids_no_site = {p.get("itemId") for p in produtos if p.get("itemId")}
    minimo = cfg["desconto_minimo_percentual"]
    verificar = cfg.get("verificar_estoque", True)
    n_busca = int(cfg.get("palavras_na_busca", 3))
    trocas, erros = 0, 0

    for p in produtos:
        codigo = p.get("codigo")
        try:
            # A) Produto novo só com o link de afiliado (cadastrado no painel).
            if not p.get("itemId") and p.get("link") and not p.get("nome"):
                preencher_por_link(p, cfg, app_id, secret, agora, ids_no_site)
                continue

            # B) Produto completo cadastrado à mão (sem itemId): o bot não mexe.
            if not p.get("itemId") and p.get("link"):
                continue

            # C) itemId informado e ainda sem dados: só preenche.
            if p.get("itemId") and not p.get("nome"):
                item = buscar_por_id(p["itemId"], app_id, secret)
                if item:
                    aplicar(p, item, cfg, agora, manter_link=bool(p.get("link")))
                    if not p.get("busca"):
                        p["busca"] = gerar_busca(item["nome"], n_busca)
                    log(f"[{codigo}] preenchido com o itemId {item['itemId']}: {item['nome'][:50]}")
                else:
                    log(f"[{codigo}] itemId {p['itemId']} não foi encontrado na API.")
                continue

            # D) Produto novo, só com palavra-chave: escolhe o melhor da busca.
            if not p.get("itemId"):
                if not p.get("busca"):
                    log(f"[{codigo}] sem link, 'busca' nem 'itemId': ignorado.")
                    continue
                novo = escolher(buscar(p["busca"], cfg, app_id, secret), p, cfg, ids_no_site)
                if novo:
                    aplicar(p, novo, cfg, agora)
                    ids_no_site.add(novo["itemId"])
                    log(f"[{codigo}] primeiro produto escolhido: R$ {novo['preco']:.2f} | {novo['nome'][:50]}")
                else:
                    log(f"[{codigo}] nenhum produto com nota/vendas/desconto suficientes para '{p['busca']}'.")
                continue

            # E) Produto que já está no site: ainda vale a pena?
            atual = buscar_por_id(p["itemId"], app_id, secret)
            promo_ok = bool(atual and atual["preco"] > 0 and atual["desconto"] >= minimo)
            motivo = None
            if p.get("trocar_agora"):
                motivo = "marcado como esgotado no painel"
            elif atual is None:
                motivo = "sumiu da API (provavelmente esgotado ou removido)"
            elif not promo_ok:
                motivo = "voltou ao preço normal"
            elif verificar and checar_estoque(p.get("shopId"), p["itemId"]) is True:
                motivo = "esgotado"

            if not motivo:
                aplicar(p, atual, cfg, agora)
                p.pop("esgotado", None)
                log(f"[{codigo}] tudo certo ({atual['desconto']}% off): R$ {atual['preco']:.2f}")
                continue

            esgotado = motivo != "voltou ao preço normal"
            log(f"[{codigo}] precisa trocar: {motivo}.")
            substituir = bool(p.get("busca")) and (not p.get("fixo") or p.get("trocar_agora"))
            if substituir and trocas >= int(cfg["max_trocas_por_execucao"]):
                log(f"[{codigo}] limite de trocas por execução atingido; fica para amanhã.")
                continue
            if atual:
                aplicar(p, atual, cfg, agora)  # atualiza o preço e marca como sem promoção
            else:
                p["em_promocao"] = False
            novo = escolher(buscar(p["busca"], cfg, app_id, secret), p, cfg, ids_no_site) if substituir else None
            if novo:
                historico.append(
                    {"data": agora, "codigo": codigo, "de": p.get("nome", ""), "para": novo["nome"],
                     "preco": novo["preco"], "motivo": motivo}
                )
                ids_no_site.add(novo["itemId"])
                for k in ("shopId", "trocar_agora", "esgotado", "erro_link", "imagem_propria"):
                    p.pop(k, None)
                aplicar(p, novo, cfg, agora)
                p["trocado_em"] = agora
                trocas += 1
                log(f"[{codigo}] TROCADO por: R$ {novo['preco']:.2f} | {novo['nome'][:50]}")
            else:
                if esgotado:
                    p["esgotado"] = True
                else:
                    p.pop("esgotado", None)
                log(f"[{codigo}] sem troca ({'produto fixo' if not substituir else 'nenhum substituto bom hoje'}); mantido.")
        except ErroAPI as e:
            erros += 1
            log(f"[{codigo}] ERRO da API: {e}")

    if produtos and erros == len(produtos):
        log("Todas as consultas falharam (confira App ID e Secret). Nada foi salvo.")
        sys.exit(1)

    site["atualizado_em"] = agora
    site["historico_trocas"] = historico[-100:]
    with open(ARQ_PRODUTOS, "w", encoding="utf-8") as f:
        json.dump(site, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"Pronto: {trocas} troca(s), {erros} erro(s).")


def linha(i):
    return (
        f"  {i['itemId']} | R$ {i['preco']:.2f} (-{i['desconto']}%) | nota {i['nota']} "
        f"| {i['vendas']} vendas | {i['nome'][:60]}"
    )


def diagnostico(alvo, cfg, app_id, secret):
    alvo = alvo.strip()
    if not alvo:
        log("Informe uma palavra-chave ou um itemId.")
        sys.exit(1)
    if alvo.isdigit():
        q = TEMPLATE_ID.replace("%ID%", alvo).replace("%CAMPOS%", CAMPOS)
        log("Resposta bruta da API para o itemId:")
        log(json.dumps(chamar_api(q, app_id, secret), indent=2, ensure_ascii=False))
        return
    itens = buscar(alvo, cfg, app_id, secret)
    log(f"{len(itens)} produtos encontrados para '{alvo}'. Primeiro resultado (bruto):")
    if itens:
        log(json.dumps(itens[0]["bruto"], indent=2, ensure_ascii=False))
    log("\nPrimeiros 15 resultados:")
    for i in itens[:15]:
        log(linha(i))
    escolhido = escolher(itens, {"busca": alvo}, cfg, set())
    log("\nO bot escolheria:")
    log(linha(escolhido) if escolhido else "  nenhum (nenhum passou nos filtros de nota, vendas e desconto)")


def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else "atualizar"
    app_id = os.environ.get("SHOPEE_APP_ID", "").strip()
    secret = os.environ.get("SHOPEE_APP_SECRET", "").strip()
    if not app_id or not secret:
        log("ERRO: defina SHOPEE_APP_ID e SHOPEE_APP_SECRET (em Settings > Secrets do GitHub).")
        sys.exit(1)
    with open(ARQ_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    try:
        if modo == "diagnostico":
            diagnostico(sys.argv[2] if len(sys.argv) > 2 else "", cfg, app_id, secret)
        else:
            atualizar(cfg, app_id, secret)
    except ErroAPI as e:
        log(f"ERRO da API: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
