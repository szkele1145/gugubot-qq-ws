package com.gugubot.chatmod;

import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.builder.LiteralArgumentBuilder;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.MutableComponent;
import net.minecraft.server.level.ServerPlayer;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

/**
 * /guguchat inject <显示名> <消息>
 * <p>
 * MCDR 插件通过控制台执行此命令，mod 收到后向所有在线玩家
 * 注入一条「原生玩家聊天包」，客户端按原版聊天渲染（带聊天头、
 * 出现在聊天框而不是 say 广播样式），从而兼容各类聊天框模组。
 * <p>
 * 权限：仅控制台/OP 2 级（防止玩家伪造他人发言）。
 */
public class GuguChatCommand {

    private static final Logger LOGGER = LogManager.getLogger("GuguChat");

    public static void register(com.mojang.brigadier.CommandDispatcher<CommandSourceStack> dispatcher) {
        dispatcher.register(LiteralArgumentBuilder.<CommandSourceStack>literal("guguchat")
            .requires(src -> src.hasPermission(2) || src.getTextName().equals("Server"))
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
        );
    }
}
