package com.gugubot.chatmod;

import net.minecraft.network.chat.ChatType;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.FilterMask;
import net.minecraft.network.chat.MessageSignature;
import net.minecraft.network.chat.SignedMessageBody;
import net.minecraft.network.protocol.game.ClientboundPlayerChatPacket;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.List;
import java.util.Locale;
import java.util.UUID;

/**
 * 原生聊天包注入器。
 * <p>
 * 把一条 QQ→MC 消息包装成 1.21.1 的 ClientboundPlayerChatPacket：
 * <ul>
 *   <li>sender UUID：<b>优先匹配在线玩家</b>（显示名 == 某在线玩家名时，
 *       借用其真实 UUID，客户端有完整档案，头像/名字全部正确，
 *       AtomChat capturePlayerChat 查 PlayerInfo 不会 NPE）；
 *       匹配不到时退回确定性派生的虚构 UUID（同一显示名稳定同 UUID，
 *       客户端仍按聊天头渲染显示名，聊天框模组按"玩家"聚合）。</li>
 *   <li>unsignedContent：未签名正文（客户端优先显示 unsignedContent，
 *       聊天头/样式与原版玩家消息一致）</li>
 *   <li>signature = MessageSignature.EMPTY（系统注入消息无签名）</li>
 *   <li>FilterMask.PASS_THROUGH（不做过滤标记）</li>
 *   <li>chatType：minecraft:chat 绑定「显示名」→ 客户端渲染
 *       "chat.type.text" 翻译键 → <显示名> 消息，AtomChat 判定为
 *       PLAYER 路由，与真玩家发言完全一致</li>
 * </ul>
 */
public class ChatInjector {

    private static final Logger LOGGER = LogManager.getLogger("GuguChat");
    private static volatile MinecraftServer server;

    /** 运行统计（/guguchat status 展示用） */
    public static final class Stats {
        public volatile long totalMessages = 0;
        public volatile long totalDelivered = 0;
        public volatile String lastMessage = null;
        public volatile long lastAtMs = 0;
    }

    private static final Stats STATS = new Stats();

    static void setServer(MinecraftServer s) {
        server = s;
    }

    /** 供 status 命令读取服务器实例 */
    public static MinecraftServer getServer() {
        return server;
    }

    public static Stats getStats() {
        return STATS;
    }

    /** 注入一条原生聊天消息，返回送达玩家数 */
    public static int inject(String displayName, String message) {
        MinecraftServer srv = server;
        if (srv == null) {
            return 0;
        }
        List<ServerPlayer> players = srv.getPlayerList().getPlayers();
        if (players.isEmpty()) {
            return 0;
        }

        UUID sender = resolveUuid(srv, displayName);
        Component content = Component.literal(message);
        // 1.21.1: ChatType.bind(ResourceKey<ChatType>, RegistryAccess, Component)
        // 渲染为 <显示名> 消息 —— 与原版玩家发言完全一致
        ChatType.Bound chatType = ChatType.bind(
                ChatType.CHAT,
                srv.registryAccess(),
                Component.literal(displayName)
        );

        // SignedMessageBody.Packed：内容 + 时间戳 + 盐 + 上一条消息签名（空列表）
        long timestamp = System.currentTimeMillis();
        long salt = 0L; // 未签名消息盐固定
        SignedMessageBody.Packed body = new SignedMessageBody.Packed(
                message, java.time.Instant.ofEpochMilli(timestamp), salt,
                new net.minecraft.network.chat.LastSeenMessages.Packed(java.util.List.of())
        );

        ClientboundPlayerChatPacket packet = new ClientboundPlayerChatPacket(
                sender,
                0,                                  // index（非重写消息用 0）
                null,                               // 1.21.1 未签名消息：signature 为 null（MessageSignature.EMPTY 已移除）
                body,
                content,                            // unsignedContent
                FilterMask.PASS_THROUGH,
                chatType
        );

        int delivered = 0;
        for (ServerPlayer p : players) {
            p.connection.send(packet);
            delivered++;
        }

        // 统计（/guguchat status 展示）
        STATS.totalMessages += 1;
        STATS.totalDelivered += delivered;
        STATS.lastMessage = "<" + displayName + "> " + (message.length() > 50 ? message.substring(0, 50) + "…" : message);
        STATS.lastAtMs = System.currentTimeMillis();

        // 同时在服务端日志里打一份（MCDR 能看到，供其他插件/日志分析）
        LOGGER.info("[GuguChat] <{}> {}", displayName, message);
        return delivered;
    }

    /**
     * 解析发送者 UUID：
     * 1. 显示名与某在线玩家（游戏名）精确匹配 → 该玩家真实 UUID（最佳：档案齐全）
     * 2. 否则确定性派生虚构 UUID（同一显示名永远相同）
     */
    private static UUID resolveUuid(MinecraftServer srv, String displayName) {
        String target = displayName == null ? "" : displayName.trim();
        if (!target.isEmpty()) {
            for (ServerPlayer p : srv.getPlayerList().getPlayers()) {
                String name = p.getGameProfile().getName();
                if (target.equalsIgnoreCase(name)) {
                    return p.getGameProfile().getId();
                }
            }
        }
        return stableUuid(target);
    }

    /** 显示名 → 稳定虚构 UUID（同一显示名永远相同，无需持久化） */
    private static UUID stableUuid(String name) {
        return UUID.nameUUIDFromBytes(("GuguChat:" + name.toLowerCase(Locale.ROOT)).getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }
}
