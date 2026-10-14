"""Radar Crise: acha o CNPJ das empresas em recuperação/falência na base aberta da Receita Federal.
Lê os nomes no banco (chave pública, só leitura), baixa os arquivos "Empresas" do CNPJ aberto,
casa pela razão social exata (sem acento e sem sufixos de recuperação) e grava data/cnpj_receita.json.
Só fica o CNPJ quando a razão social é única na Receita (sem ambiguidade)."""
import csv, io, json, os, re, sys, time, unicodedata, urllib.request, zipfile, datetime

API = "https://ryhyvzwqtktaaonznizn.supabase.co/rest/v1/crise_processos"
KEY = "sb_publishable_Ji31DC1ihqZTR7gmLeYTLw_GHgBjWN0"
BASES = ["https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/",
         "https://dadosabertos.rfb.gov.br/CNPJ/dados_abertos_cnpj/"]
UA = {"User-Agent": "Mozilla/5.0 RadarCrise/0.3 (dados abertos CNPJ)"}
status = {"inicio": datetime.datetime.utcnow().isoformat() + "Z"}

def norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().upper()
    s = re.sub(r"\b(EM RECUPERACAO JUDICIAL|EM RECUPERACAO EXTRAJUDICIAL|MASSA FALIDA( DE| DA| DO)?|FALIDA|FALIDO)\b", " ", s)
    s = re.sub(r"\bS\s*/?\s*A\b\.?", " SA ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def dv(base12):
    def calc(b, pesos):
        r = sum(int(x) * p for x, p in zip(b, pesos)) % 11
        return "0" if r < 2 else str(11 - r)
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = calc(base12, p1); d2 = calc(base12 + d1, [6] + p1)
    return base12 + d1 + d2

def get(url, tries=2):
    for k in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40)
        except Exception as e:
            print("tentativa", k + 1, url, e, flush=True); time.sleep(10 * (k + 1))
    raise RuntimeError("falhou " + url)

# 1) nomes que precisam de CNPJ
nomes = {}
off = 0
while True:
    u = f"{API}?select=id,devedor&devedor=not.is.null&cnpj=is.null&limit=1000&offset={off}"
    rows = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={**UA, "apikey": KEY})))
    for r in rows:
        n = re.sub(r"\s+e mais \d+$", "", r["devedor"])
        nomes.setdefault(norm(n), []).append(r["id"])
    if len(rows) < 1000: break
    off += 1000
nomes.pop("", None)
status["nomes"] = len(nomes)
print("nomes a casar:", len(nomes), flush=True)

# 2) mês mais recente publicado (a Receita mudou para um compartilhamento Nextcloud; tenta os caminhos conhecidos)
import base64
TOKEN = "YggdBLfdninEJX9"
NC = "https://arquivos.receitafederal.gov.br"
base = mes = None
modo = None
try:
    req = urllib.request.Request(NC + "/public.php/webdav/", method="PROPFIND",
        headers={**UA, "Depth": "1", "Authorization": "Basic " + base64.b64encode((TOKEN + ":").encode()).decode()})
    xml = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
    meses = sorted(set(re.findall(r"/public\.php/webdav/(\d{4}-\d{2})/", xml)))
    status["nextcloud_meses"] = meses[-3:]
    if meses: base, mes, modo = NC, meses[-1], "nextcloud"
except Exception as e:
    status["nextcloud_erro"] = str(e)[:200]; print("nextcloud falhou", e, flush=True)
if not base:
    for b in BASES:
        try:
            html = get(b, 2).read().decode("latin-1")
            meses = sorted(set(re.findall(r'href="(\d{4}-\d{2})/?"', html)))
            if meses: base, mes, modo = b, meses[-1], "pasta"; break
        except Exception as e:
            status.setdefault("pastas_erro", []).append(str(e)[:120]); print("lista falhou", b, e)

def url_arquivo(nome):
    if modo == "nextcloud":
        return f"{NC}/public.php/webdav/{mes}/{nome}"
    return f"{base}{mes}/{nome}"

def abrir(url):
    h = dict(UA)
    if modo == "nextcloud": h["Authorization"] = "Basic " + base64.b64encode((TOKEN + ":").encode()).decode()
    for k in range(4):
        try: return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=180)
        except Exception as e: print("tentativa", k + 1, url, e, flush=True); time.sleep(15 * (k + 1))
    raise RuntimeError("falhou " + url)

if not base:
    status["erro"] = "não achei a pasta da Receita"
else:
    status["fonte"] = f"{modo}: {base} {mes}"
    achados = {}   # nome -> set(cnpj_basico)
    for i in range(10):
        url = url_arquivo(f"Empresas{i}.zip")
        t0 = time.time()
        try:
            tmp = f"/tmp/emp{i}.zip"
            with abrir(url) as r, open(tmp, "wb") as f:
                while True:
                    blk = r.read(1 << 20)
                    if not blk: break
                    f.write(blk)
            with zipfile.ZipFile(tmp) as z:
                nome_arq = z.namelist()[0]
                with z.open(nome_arq) as fh:
                    for row in csv.reader(io.TextIOWrapper(fh, encoding="latin-1", newline=""), delimiter=";"):
                        if len(row) < 2: continue
                        k = norm(row[1])
                        if k in nomes: achados.setdefault(k, set()).add(row[0])
            os.remove(tmp)
            print(f"Empresas{i}: ok em {time.time()-t0:.0f}s, casados até agora {len(achados)}", flush=True)
        except Exception as e:
            status.setdefault("falhas", []).append(f"Empresas{i}: {e}")
            print("falhou", url, e, flush=True)
    saida = []
    for k, cs in achados.items():
        if len(cs) != 1: continue        # razão social repetida na Receita: não arrisca
        b = next(iter(cs)).zfill(8)
        cnpj = dv(b + "0001")
        fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
        for pid in nomes[k]: saida.append({"id": pid, "cnpj": fmt})
    status["casados_unicos"] = len(saida)
    status["ambiguos"] = sum(1 for cs in achados.values() if len(cs) > 1)
    os.makedirs("data", exist_ok=True)
    json.dump(saida, open("data/cnpj_receita.json", "w"), ensure_ascii=False)
status["fim"] = datetime.datetime.utcnow().isoformat() + "Z"
os.makedirs("data", exist_ok=True)
json.dump(status, open("data/cnpj_status.json", "w"), ensure_ascii=False, indent=1)
print(status)
