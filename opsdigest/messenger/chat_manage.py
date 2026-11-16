"""Explicit bulk membership operations using the existing Telegram connection."""
import asyncio


async def execute(live, ids, action, username):
    from telethon import functions, types
    from .live import LiveError, human
    user = None
    if action == 'invite':
        user = await live.client.get_entity(username)
        if not isinstance(user, types.User) or user.bot or user.deleted:
            raise LiveError('Укажите ник действующего пользователя Telegram')
    rows, halted = [], False
    for cid in ids:
        row = dict(id=cid, title=str(cid), status='failed', detail='', link='')
        rows.append(row)
        if halted:
            row.update(status='skipped', detail='Ограничение Telegram: повторите позже')
            continue
        ent = None
        try:
            ent = await live.client.get_entity(cid)
            row['title'] = getattr(ent, 'title', str(cid))
            if not isinstance(ent, (types.Chat, types.Channel)):
                row.update(status='skipped', detail='Доступно только для групп и каналов')
                continue
            if action == 'leave':
                request = (functions.channels.LeaveChannelRequest(ent) if isinstance(ent, types.Channel)
                           else functions.messages.DeleteChatUserRequest(ent.id, types.InputUserSelf()))
                await live.client(request, flood_sleep_threshold=0)
                row.update(status='success', detail='Вы вышли из чата')
            else:
                try:
                    await live.client.get_permissions(ent, user)
                except Exception as exc:
                    if type(exc).__name__ != 'UserNotParticipantError':
                        raise
                else:
                    row.update(status='already', detail='Участник уже в чате')
                    continue
                request = (functions.channels.InviteToChannelRequest(ent, [user]) if isinstance(ent, types.Channel)
                           else functions.messages.AddChatUserRequest(ent.id, user, 0))
                result = await live.client(request, flood_sleep_threshold=0)
                if getattr(result, 'missing_invitees', None):
                    row['detail'] = 'Telegram не добавил участника из-за ограничений приватности'
                else:
                    await live.client.get_permissions(ent, user)
                    row.update(status='success', detail='Участник добавлен, присутствие подтверждено')
        except Exception as exc:
            name = type(exc).__name__
            row['detail'] = {
                'ChatAdminRequiredError': 'Нет прав: попросите администратора добавить участника',
                'UserPrivacyRestrictedError': 'Приватность запрещает добавление: передайте ссылку участнику',
                'UserNotMutualContactError': 'Нужен взаимный контакт или помощь администратора',
                'UserAlreadyParticipantError': 'Участник уже в чате',
                'UserNotParticipantError': 'Присутствие не подтверждено: проверьте через администратора',
            }.get(name, human(exc))
            if name == 'UserAlreadyParticipantError':
                row['status'] = 'already'
            halted = name in ('FloodWaitError', 'PeerFloodError')
        if action == 'invite' and row['status'] == 'failed':
            name = getattr(ent, 'username', None)
            if name:
                row['link'] = 'https://t.me/' + name
            else:
                row['detail'] += '; запросите ссылку на вступление у администратора'
        await asyncio.sleep(1)
    live.push({'type': 'dialogs'})
    return dict(action=action, username=username, rows=rows)
