# ✈️ Monitor de passagens GRU → EUA → Tóquio

Procura no Google Flights viagens **GRU → parada de 2 dias nos EUA → Tóquio → GRU** e manda
um e-mail quando o total fica abaixo do preço máximo.

**Custo total: R$ 0/mês** (GitHub Actions em repo público + Gmail SMTP)

---

## Como funciona

```
GitHub Actions (a cada 2h)
      │
      ▼
Google Flights  ──►  lote de 50 buscas  ──►  histórico de preços
(fast-flights)       (bilhetes só de ida      (cache do Actions)
                      e ida e volta)                │
                                                    ▼
                               monta as combinações por parada e datas
                                                    │
                                     total ≤ preço máximo?
                                      │                 │
                                     SIM               NÃO
                                      │                 │
                         busca de novo e confirma    sem e-mail
                                      │
                                 Gmail SMTP
```

- Para cada cidade de parada, data de saída e tempo no Japão, testa 5 formas de comprar
  com bilhetes separados (3 ou 4 só de ida, 2 idas e voltas, ou misturas) e fica com a
  mais barata.
- As cerca de 650 buscas necessárias são divididas entre as execuções; uma combinação só
  conta se todos os preços tiverem até 48 h.
- O e-mail só sai depois de confirmar o preço na hora, com o link de cada bilhete. A mesma
  combinação só é avisada de novo se cair pelo menos R$ 150.
- Cada execução mostra as 5 melhores combinações na página dela, em **Actions**.

## Configurar

### 1. Gmail App Password

> ⚠️ Use App Password, não sua senha do Gmail

1. Acesse [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   (precisa da verificação em duas etapas ativa)
2. Crie uma senha e copie os 16 caracteres

### 2. Secrets do repositório

Em **Settings → Secrets and variables → Actions → Secrets**, ou pelo terminal:

```bash
gh secret set EMAIL_FROM
gh secret set EMAIL_PASS
gh secret set EMAIL_TO
```

### 3. Critérios

Edite o [`config.toml`](config.toml) ou, sem mexer no código, crie **Variables** em
**Settings → Secrets and variables → Actions → Variables**:

| Variable | Exemplo | O que muda |
|---|---|---|
| `PRECO_MAXIMO` | `6000` | total em R$ para disparar o e-mail |
| `SAIDA_DE` / `SAIDA_ATE` | `2027-03-15` / `2027-03-21` | janela de saída de Guarulhos |
| `DIAS_NO_JAPAO` | `10,12,14` | tempos de estadia testados |
| `PARADAS` | `LAX,SFO,HNL` | cidades dos EUA aceitas para a parada |

### 4. Testar

Em **Actions → Monitor de passagens → Run workflow**:
- marque **"Só enviar um e-mail de teste"** para conferir os Secrets;
- ou rode uma busca normal, escolhendo quantas buscas fazer e se manda e-mail.

## Rodar no seu computador

```bash
cp .env.example .env               # preencha
uv run python monitor.py --testar-email
uv run python monitor.py --sem-email --paradas LAX --max-consultas 20
```

## Limitações

- Usa a página pública do Google Flights (sem API oficial): pode quebrar se o Google mudar
  algo ou bloquear buscas. Se todas as buscas falharem, a execução fica vermelha e o GitHub
  avisa por e-mail.
- Ainda não busca multi-city num bilhete só (GRU→LAX, LAX→Tóquio, Tóquio→GRU), que pode
  sair mais barato que bilhetes separados.
- Bilhetes separados não protegem contra atraso entre eles; a volta prevê uma noite na
  cidade da parada. A parada nos EUA exige visto americano.
- O GitHub desliga agendamentos de repositórios públicos após 60 dias sem commits; o
  workflow faz um commit vazio a cada 45 dias para evitar isso.
