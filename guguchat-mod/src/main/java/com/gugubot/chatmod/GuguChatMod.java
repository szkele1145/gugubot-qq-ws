package com.gugubot.chatmod;

import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.RegisterCommandsEvent;
import net.neoforged.neoforge.event.server.ServerStartedEvent;
import net.neoforged.neoforge.event.server.ServerStoppingEvent;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

@Mod(GuguChatMod.MODID)
public class GuguChatMod {

    public static final String MODID = "guguchat";
    public static final Logger LOGGER = LogManager.getLogger("GuguChat");

    public GuguChatMod(IEventBus modBus) {
        // 注册服务端事件
        NeoForge.EVENT_BUS.addListener(this::onRegisterCommands);
        NeoForge.EVENT_BUS.addListener(this::onServerStarted);
        NeoForge.EVENT_BUS.addListener(this::onServerStopping);
        LOGGER.info("GuguChatMod 已加载：等待 /guguchat 注入命令");
    }

    private void onRegisterCommands(RegisterCommandsEvent event) {
        GuguChatCommand.register(event.getDispatcher());
        LOGGER.info("命令已注册：/guguchat inject | /guguchat status");
    }

    private void onServerStarted(ServerStartedEvent event) {
        ChatInjector.setServer(event.getServer());
        LOGGER.info("GuguChatMod 已就绪：/guguchat inject <显示名> <消息> | /guguchat status");
    }

    private void onServerStopping(ServerStoppingEvent event) {
        ChatInjector.setServer(null);
    }
}
