package com.gugubot.chatmod;

import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.builder.LiteralArgumentBuilder;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.List;

/**
 * /guguchat inject <整行消息>   —— MCDR 插件注入入口（仅作 v2.2.7 之前的
 *                              兼容备用通道；v2.2.7+ 插件改走原版 tellraw，
 *                              本命令不再被默认调用）
 * /guguchat status              —— 查看 mod 状态（在线玩家/注入计数/最近注入）
 * <p>
 * inject 由 MCDR 插件通过控制台 stdin 执行，mod 收到后向所有在线玩家
 * 注入一条「系统消息包」（ClientboundSystemChatPacket）。系统消息通道
 * 不做聊天签名验证，离线/未签名服务器不会出现"聊天验证错误"。
 * <p>
 * 权限：仅控制台/OP 2 级（防止玩家伪造他人发言）。
 */
public class GuguChatCommand {

    private static final Logger LOGGER = LogManager.getLogger("GuguChat");

    public static void register(com.mojang.brigadier.CommandDispatcher<CommandSourceStack> dispatcher) {
        dispatcher.register(LiteralArgumentBuilder.<CommandSourceStack>literal("guguchat")
            .requires(src -> src.hasPermission(2) || src.getTextName().equals("Server"))
            // ---- inject <整行消息> ----
            .then(Commands.literal("inject")
                .then(Commands.argument("rawLine", StringArgumentType.greedyString())
                    .executes(ctx -> {
                        String rawLine = StringArgumentType.getString(ctx, "rawLine");
                        int n = ChatInjector.inject(rawLine);
                        if (n == 0) {
                            ctx.getSource().sendFailure(
                                Component.literal("GuguChat: 没有在线玩家，消息未投递（此消息不会出现在游戏里）"));
                            return 0;
                        }
                        return com.mojang.brigadier.Command.SINGLE_SUCCESS;
                    })
                )
            )
            // ---- status：运行状态 ----
            .then(Commands.literal("status")
                .executes(ctx -> {
                    MinecraftServer srv = ChatInjector.getServer();
                    if (srv == null) {
                        ctx.getSource().sendFailure(Component.literal("GuguChat: 服务器未就绪"));
                        return 0;
                    }
                    List<ServerPlayer> players = srv.getPlayerList().getPlayers();
                    ChatInjector.Stats stats = ChatInjector.getStats();
                    Component report = Component.literal(
                        String.format(
                            "GuguChat v1.0.0 状态:\n" +
                            "  在线玩家: %d\n" +
                            "  累计注入: %d 条 (送达 %d 人次)\n" +
                            "  最近注入: %s\n" +
                            "  最后注入距今: %s",
                            players.size(),
                            stats.totalMessages, stats.totalDelivered,
                            stats.lastMessage != null ? stats.lastMessage : "(无)",
                            stats.lastAtMs > 0 ? (System.currentTimeMillis() - stats.lastAtMs) / 1000 + " 秒前" : "(无)"
                        )
                    );
                    ctx.getSource().sendSuccess(() -> report, false);
                    return com.mojang.brigadier.Command.SINGLE_SUCCESS;
                })
            )
        );
    }
}
