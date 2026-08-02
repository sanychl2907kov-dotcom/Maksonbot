# ========== ЛОГИКА ДОБАВЛЕНИЯ УЧАСТНИКОВ ==========
# Добавляем автора всегда
await thread.add_user(i.user)

# Если тикет ПРЕДЛОЖЕНИЕ — добавляем только автора и AUTHORIZED_USER_ID
if self.ticket_type == "Предложение":
    # Добавляем только тебя
    owner = i.guild.get_member(AUTHORIZED_USER_ID)
    if owner:
        try:
            await thread.add_user(owner)
        except:
            pass
    # НЕ добавляем модераторов и другие роли
else:
    # Для ЖАЛОБЫ и других типов — добавляем модераторов
    for role_id in SUPPORT_ROLE_IDS:
        role = i.guild.get_role(role_id)
        if role:
            for member in role.members:
                try:
                    await thread.add_user(member)
                except:
                    pass
    
    # Также добавляем тебя (на всякий случай)
    owner = i.guild.get_member(AUTHORIZED_USER_ID)
    if owner:
        try:
            await thread.add_user(owner)
        except:
            pass
