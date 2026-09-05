import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import file_association


class TestExecutableCommand(unittest.TestCase):
    def test_command_contains_placeholder(self):
        cmd = file_association.executable_command()
        self.assertTrue(cmd.endswith('"%1"'))
        # 路徑須以雙引號包住，避免含空白的路徑被拆成多個參數
        self.assertTrue(cmd.count('"') >= 4)

    def test_command_targets_entry_script(self):
        # 命令須指向真正的入口腳本 MultiSocksDownloader.py（不是 ui.py）
        cmd = file_association.executable_command()
        self.assertIn("MultiSocksDownloader.py", cmd)

    def test_icon_source_ends_with_zero_index(self):
        icon = file_association.icon_source()
        self.assertTrue(icon.endswith(',0'))
        self.assertTrue(icon.startswith('"'))


class TestIsRegistered(unittest.TestCase):
    def test_returns_bool_without_raising(self):
        # 只驗證回傳布林值且不拋例外；實際值依環境登錄檔而定
        result = file_association.is_registered()
        self.assertIsInstance(result, bool)


class TestProgId(unittest.TestCase):
    def test_prog_id_stable(self):
        self.assertEqual(file_association.PROG_ID, "MultiSocksDownloader.torrent")
        self.assertEqual(file_association.EXTENSION, ".torrent")


# 以 mock 取代 winreg 個別函式，驗證註冊/移除寫入的鍵值，不實際動到登錄檔。
class TestRegisterWithMock(unittest.TestCase):
    def test_register_writes_expected_keys(self):
        with mock.patch.object(file_association.winreg, "CreateKeyEx",
                               return_value=mock.MagicMock()) as create, \
             mock.patch.object(file_association.winreg, "SetValueEx") as setval, \
             mock.patch.object(file_association, "_notify_shell"):
            self.assertTrue(file_association.register())

        # CreateKeyEx(root, subkey, ...) 與 SetValueEx(key, name, 0, REG_SZ, value)
        # 兩者在 register() 內是 1:1 依序出現，zip 對應即可取得 subkey -> value。
        subkeys = [c.args[1] for c in create.call_args_list]
        values = [c.args[4] for c in setval.call_args_list]
        by_key = dict(zip(subkeys, values))

        _ck = file_association._classes_key
        _fk = file_association._fileexts_key
        # 副檔名預設值指向 ProgID
        self.assertEqual(by_key[_ck(file_association.EXTENSION)],
                         file_association.PROG_ID)
        # 開啟命令包含 "%1" 佔位符
        self.assertIn("%1", by_key[_ck(file_association.PROG_ID, "shell", "open", "command")])
        # 圖示指向 index 0（內嵌於執行檔）
        self.assertTrue(
            by_key[_ck(file_association.PROG_ID, "DefaultIcon")].endswith(",0"))
        # Win10/11：ProgID 須一併寫進 FileExts 的 OpenWithProgids，
        # 讓程式出現在「開啟方式」清單
        self.assertEqual(
            by_key[_fk(file_association.EXTENSION, "OpenWithProgids")], "")

    def test_unregister_when_not_registered_does_not_raise(self):
        with mock.patch.object(file_association.winreg, "OpenKey",
                               side_effect=FileNotFoundError), \
             mock.patch.object(file_association, "_delete_tree"), \
             mock.patch.object(file_association, "_notify_shell"):
            # 尚未註冊時移除不應拋出例外
            file_association.unregister()


class TestTriggerSystemDialog(unittest.TestCase):
    def test_returns_false_on_non_windows(self):
        with mock.patch.object(file_association.sys, "platform", "linux"):
            self.assertFalse(file_association.trigger_system_dialog())

    def test_triggers_openas_and_succeeds(self):
        shell = mock.MagicMock()
        shell.ShellExecuteW.return_value = 42
        with mock.patch.object(file_association, "ctypes") as ctypes_mock, \
             mock.patch.object(file_association.tempfile, "mkstemp",
                               return_value=(1, "tmp.torrent")), \
             mock.patch.object(file_association.os, "close"), \
             mock.patch.object(file_association.threading, "Thread"):
            ctypes_mock.windll.shell32 = shell
            self.assertTrue(file_association.trigger_system_dialog())
            shell.ShellExecuteW.assert_called_once()
            # 必須用 openas 動詞，才會彈出「你要如何開啟此檔案」對話框
            self.assertEqual(shell.ShellExecuteW.call_args.args[1], "openas")


if __name__ == "__main__":
    unittest.main()
