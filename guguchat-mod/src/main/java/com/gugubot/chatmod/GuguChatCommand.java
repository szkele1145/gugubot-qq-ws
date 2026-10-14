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
 * /guguchat inject <显示名> <消息>   —— MCDR 插件注入入口
 * /guguchat status                   —— 查看 mod 状态（在线玩家/注入计数/最近注入）
 * <p>
 * inject 由 MCDR 插件通过控制台 stdin 执行，mod 收到后向所有在线玩家
 * 注入一条「原生玩家聊天包」（ClientboundPlayerChatPacket），客户端按
 * 原版聊天渲染，兼容 AtomChat 等聊天框模组。
 * <p>
 * 权限：仅控制台/OP 2 级（防止玩家伪造他人发言）。
 */
public class GuguChatCommand {

    private static final Logger LOGGER = LogManager.getLogger("GuguChat");

    public static void register(com.mojang.brigadier.CommandDispatcher<CommandSourceStack> dispatcher) {
        dispatcher.register(LiteralArgumentBuilder.<CommandSourceStack>literal("guguchat")
            .requires(src -> src.hasPermission(2) || src.getTextName().equals("Server"))
            // ---- inject <显示名> <消息> ----
            .then(Commands.literal("inject")
                .then(Commands.argument("display", StringArgumentType.string())
                    .then(Commands.argument("message", StringArgumentType.greedyString())
                        .executes(ctx -> {
                            String display = StringArgumentType.getString(ctx, "display");
                            String message = StringArgumentType.getString(ctx, "message");
                            int n = ChatInjector.inject(display, message);
                            if (n == 0) {
                                ctx.getSource().sendFailure(
                                    Component.literal("GuguChat: 没有在线玩家，消息未投递（此消息不会出现在游戏里）"));
                                return 0;
                            }
                            return com.mojang.brigadier.Command.SINGLE_SUCCESS;
                        })
                    )
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
