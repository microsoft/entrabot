#!/usr/bin/env python3
"""Diagnostic script to test Teams chat creation and inspect the result.

Run: python scripts/diagnose-chat.py

This bypasses the MCP server entirely and tests the chat creation
directly against the Graph API, logging every detail.
"""

from __future__ import annotations

import asyncio
import sys

# Ensure the source is importable
sys.path.insert(0, "src")

from entrabot.config import EntraBotConfig
from entrabot.tools.teams import (
    GRAPH_BASE,
    acquire_agent_user_token,
    create_or_find_chat,
    send,
)


async def main() -> None:
    config = EntraBotConfig.from_env()

    print("=" * 60)
    print("EntraBot Chat Diagnostic")
    print("=" * 60)
    print(f"Tenant ID:          {config.tenant_id}")
    print(f"Agent User ID:      {config.agent_user_id}")
    print(f"Agent User UPN:     {config.agent_user_upn}")
    print(f"Human User IDs:     {config.human_user_ids}")
    print(f"Human User Types:   {config.human_user_types}")
    print(f"Human User Mails:   {config.human_user_mails}")
    print(f"Human Tenant IDs:   {config.human_user_tenant_ids}")
    print()

    # Step 1: Acquire token
    print("[1/4] Acquiring Agent User token (three-hop flow)...")
    try:
        token = acquire_agent_user_token(config)
        print(f"  ✅ Token acquired (length={len(token)})")
    except Exception as e:
        print(f"  ❌ Token acquisition failed: {e}")
        return

    # Step 2: Create chat
    print()
    print("[2/4] Creating chat...")
    print(f"  human_user_ids:   {config.human_user_ids}")
    print(f"  human_user_types: {config.human_user_types}")
    print(f"  agent_user_id:    {config.agent_user_id}")
    try:
        chat = await create_or_find_chat(
            token=token,
            human_user_ids=config.human_user_ids,
            agent_user_id=config.agent_user_id,
            human_user_tenant_ids=config.human_user_tenant_ids,
            human_user_mails=config.human_user_mails,
            human_user_types=config.human_user_types,
        )
        chat_id = chat["chat_id"]
        print(f"  ✅ Chat created: {chat_id}")
        print(f"  Created at: {chat.get('created_at')}")
    except Exception as e:
        print(f"  ❌ Chat creation failed: {e}")
        return

    # Step 3: Inspect chat members via GET /chats/{id}/members
    print()
    print("[3/4] Inspecting chat members...")
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GRAPH_BASE}/chats/{chat_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"$expand": "members"},
        )
        if resp.status_code == 200:
            chat_detail = resp.json()
            print(f"  Chat type: {chat_detail.get('chatType')}")
            print(f"  Topic:     {chat_detail.get('topic')}")
            members = chat_detail.get("members", [])
            print(f"  Members ({len(members)}):")
            for m in members:
                print(f"    - {m.get('displayName', '?')}")
                print(f"      roles:    {m.get('roles', [])}")
                print(f"      tenantId: {m.get('tenantId', 'N/A')}")
                print(f"      userId:   {m.get('userId', 'N/A')}")
                print(f"      email:    {m.get('email', 'N/A')}")
        else:
            print(f"  ⚠️  GET /chats/{chat_id} returned {resp.status_code}")
            print(f"  Body: {resp.text[:500]}")

        # Also try members endpoint directly
        resp2 = await client.get(
            f"{GRAPH_BASE}/chats/{chat_id}/members",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp2.status_code == 200:
            members2 = resp2.json().get("value", [])
            print(f"\n  Members via /members endpoint ({len(members2)}):")
            for m in members2:
                print(
                    f"    - {m.get('displayName', '?')} | "
                    f"roles={m.get('roles')} | tenant={m.get('tenantId')}"
                )
        else:
            print(f"  ⚠️  GET /members returned {resp2.status_code}: {resp2.text[:200]}")

    # Step 4: Send test message
    print()
    print("[4/4] Sending test message...")
    try:
        msg = await send(
            chat_id=chat_id,
            message="🔧 Diagnostic test message from diagnose-chat.py",
            token=token,
        )
        print(f"  ✅ Message sent: {msg['message_id']}")
        print(f"  Sent at: {msg.get('sent_at')}")
    except Exception as e:
        print(f"  ❌ Send failed: {e}")

    print()
    print("=" * 60)
    print("Done. Check your Teams client for the message.")
    print("If using a B2B guest account, make sure you've switched")
    print("to the werner.ac organization in your Teams client.")
    print("=" * 60)


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")
    asyncio.run(main())
