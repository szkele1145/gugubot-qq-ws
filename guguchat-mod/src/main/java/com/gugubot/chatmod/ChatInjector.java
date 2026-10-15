package com.gugubot.chatmod;

import net.minecraft.network.chat.Component;
import net.minecraft.network.protocol.game.ClientboundSystemChatPacket;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.List;

/**
 * 系统聊天包注入器（v2）。
 * <p>
 * 把 QQ→MC 消息以 ClientboundSystemChatPacket 广播：
 * <ul>
 *   <li>整条消息（[QQ转发]名字:内容）作为纯文本注入——所见即所得，
 *       不依赖聊天头/玩家档案渲染</li>
 *   <li>系统消息通道不做签名验证——彻底规避 offline 服务器上
 *       未签名 PlayerChat 包触发的"聊天验证错误"</li>
 *   <li>AtomChat 按系统消息展示（不出头像，但名字/内容完整可见）</li>
 * </ul>
 * v1 的 PlayerChat 方案（聊天头渲染+头像）保留在 git 历史中，
 * 签名验证问题在 online-mode + enforce-secure-profile=true 的
 * 服务器上才可复用。
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

    /**
     * 注入一条聊天消息，返回送达玩家数。
     *
     * @param rawLine 完整的消息行（插件侧已拼好 [QQ转发]名字:内容）。
     *                旧签名 (display, message) 兼容：两参时自动拼
     *                "<display> message"（旧版插件调用不炸）。
     */
    public static int inject(String rawLine) {
        MinecraftServer srv = server;
        if (srv == null) {
            return 0;
        }
        List<ServerPlayer> players = srv.getPlayerList().getPlayers();
        if (players.isEmpty()) {
            return 0;
        }

        Component content = Component.literal(rawLine);
        ClientboundSystemChatPacket packet = new ClientboundSystemChatPacket(content, false);

        int delivered = 0;
        for (ServerPlayer p : players) {
            p.connection.send(packet);
            delivered++;
        }

        STATS.totalMessages += 1;
        STATS.totalDelivered += delivered;
        String logMsg = rawLine.length() > 100 ? rawLine.substring(0, 100) + "…" : rawLine;
        STATS.lastMessage = logMsg;
        STATS.lastAtMs = System.currentTimeMillis();

        LOGGER.info("[GuguChat] {}", logMsg);
        return delivered;
    }

    /** 旧签名兼容：display + message → "<display> message" 单行注入 */
    public static int inject(String displayName, String message) {
        return inject("<" + displayName + "> " + message);
    }
}
