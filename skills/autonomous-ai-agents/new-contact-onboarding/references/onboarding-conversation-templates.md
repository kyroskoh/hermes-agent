# Onboarding Conversation Templates

Copy-paste ready templates for each platform. Pick the one matching the inbound platform; adapt the `[display_name]` placeholder.

---

## WhatsApp (DMs)

### First inbound, has display_name

```
Hi [display_name] 👋 I think this is our first time chatting.

I'm kyroskoh_bot — Kyros's assistant. Mind introducing yourself
so I know who I'm talking to next time?

• What's your name (or what should I call you)?
• How do you know Kyros? (friend / family / work / other)
```

### First inbound, no display_name

```
Hi there 👋 first time we chat as far as I can tell.

I'm kyroskoh_bot, Kyros's assistant. Who am I speaking with?
```

### If they ask a question before introducing themselves

Answer the question first, then add at the end:

```
(Quick aside — I'm kyroskoh_bot. First time we chat, so I want
to make sure I address you right next time. What's your name,
or what should I call you?)
```

### If they decline to introduce themselves

```
No worries — I'll just address you by [display_name / your
contact name] until you tell me otherwise. Ping me whenever
you need a hand with anything.
```

Then register only the minimum: peer card gets a single
"self-introduced peer; declined to identify. Display name: …"
The peer stays OUT of `KNOWN_ALIASES`.

---

## Telegram (DMs)

### First inbound, no display_name

```
Hey — kyroskoh_bot here. Looks like this is our first DM,
so I want to make sure I address you right going forward.

What's your name (or what should I call you)?
How do you know Kyros?
```

### If the user has a Telegram username but no first name visible

```
Hey @<username> — kyroskoh_bot here. First DM from you that
I can see. Mind telling me your name so I don't have to keep
saying "@<username>"? 🙂
```

---

## Discord (server DMs)

### First inbound

```
Hey <@display_name> — I'm kyroskoh_bot, Kyros's assistant.
Looks like this is our first conversation. What's your name,
and how do you know Kyros?
```

---

## Web UI / Hermes dashboard

### First chat message

```
Hi! I'm kyroskoh_bot. First time we've talked as far as I can tell.

Mind introducing yourself? I just want to make sure I have the
right name for next time.

• Name / what to call you
• How you know Kyros (friend / family / colleague / other)
```

---

## Multi-language variants

### Mandarin (中文)

```
嗨 [display_name] 👋 看起来是我们第一次聊天。

我是 kyroskoh_bot，Kyros 的助手。方便自我介绍一下吗？
这样下次我就能用你喜欢的名字称呼你了。
```

### Malay (Bahasa Melayu)

```
Hai [display_name] 👋 nampaknya ini pertama kali kita berbual.

Saya kyroskoh_bot, pembantu Kyros. Boleh perkenalkan diri
supaya saya tahu siapa yang saya sedang bercakap dengan?
```

### Tagalog

```
Kumusta [display_name] 👋 parang first time natin mag-chat.

Ako si kyroskoh_bot, assistant ni Kyros. Pwede mo bang
magpakilala para alam ko kung sino kausap ko next time?
```

---

## Common clarifications to be ready for

| They say | Bot responds |
|---|---|
| "I'm Bille" | "Got it — Bille. Thanks for the intro. What do you usually need help with?" |
| "Kyros gave me your number" | "Cool — and what's your name, so I don't keep saying 'hey you'?" |
| "Who are you?" | "I'm kyroskoh_bot — Kyros's AI assistant running on his server. I'm here if you need a hand with anything from infrastructure to a quick lookup." |
| "I'm [name], his [relationship]" | "Nice to meet you, [name]. I'm kyroskoh_bot. Whenever you need something, ping me — I'll do my best to help." |
| "Why do you need my name?" | "Just so I address you right next time and remember what we've talked about. I won't share anything with anyone without Kyros's say-so." |

---

## After the conversation

Persist the identity. See the main SKILL.md, Step 4. Three writes:

1. **Honcho peer card** — `PUT /v3/.../peers/kyroskoh_bot/card?target=<peer_id>`
2. **Honcho conclusion** — `honcho_conclude(peer=<peer_id>, conclusion="...")`
3. **`KNOWN_ALIASES`** — append to `honcho-peer-roster/scripts/scan_peers.py` AND the SKILL.md body
