# Peer

**Peer is a non-custodial, privacy-preserving peer-to-peer marketplace for moving between fiat and crypto.** Buy and sell USDC directly with other people over the payment rails you already use, including Venmo, PayPal, Cash App, Zelle, Revolut, and Wise. No middleman ever takes custody of your money.

Peer is built on the ZKP2P protocol, which started as an Ethereum Foundation PSE-supported research project and now settles real peer-to-peer volume on Base.

**[Try Peer](https://peer.xyz)** | **[Docs](https://docs.peer.xyz)** | **[X](https://x.com/peerxyz)** | **[Telegram](https://t.me/+XDj9FNnW-xs5ODNl)** | **[Discord](https://discord.gg/h3rzP79jj3)**

## How it works

1. **Sellers post liquidity.** USDC is locked in the protocol's smart contracts onchain, at a spread the seller sets. No company holds the funds.
2. **A buyer takes an order** and pays the seller directly on the fiat rail. The money moves peer to peer, never through us.
3. **The payment is verified cryptographically.** A payment attestation is checked against the payment platform's own data, either from a buyer-generated proof or inside an attested TEE, and the contract releases the USDC to the buyer onchain.

The result: direct fiat-to-crypto trades with real payment verification instead of screenshots, support tickets, or a custodian in the middle.

## Start here

| Repo | What it is |
|---|---|
| [zkp2p-contracts](https://github.com/zkp2p/zkp2p-contracts) | Core protocol smart contracts on Base |
| [zkp2p-client-sdk](https://github.com/zkp2p/zkp2p-client-sdk) | Browser-first TypeScript SDK for building on the protocol |
| [peer-cash](https://github.com/zkp2p/peer-cash) | Peer Cash SDK: cash out Base USDC to fiat, built agent-first |
| [peer-examples](https://github.com/zkp2p/peer-examples) | Open-source integration examples and templates |
| [zkp2p-skills](https://github.com/zkp2p/zkp2p-skills) | Agent Skills for AI agents to use the protocol on Base |
| [zkp2p-v1-monorepo](https://github.com/zkp2p/zkp2p-v1-monorepo) | The original ZKP2P V1, preserved for history |

Building an onramp, offramp, or cashout flow into your product? Start with the [developer docs](https://docs.peer.xyz/developer/sdk) or come talk to us in [Telegram](https://t.me/+XDj9FNnW-xs5ODNl).
