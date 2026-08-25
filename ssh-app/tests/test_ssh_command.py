import unittest

from ssh_command import SSHCommandError, parse_ssh_command


class SSHCommandParserTests(unittest.TestCase):
    def test_dynamic_forward(self) -> None:
        command = parse_ssh_command("ssh -D 1080 root@47.88.77.200")
        self.assertEqual(command.host, "47.88.77.200")
        self.assertEqual(command.username, "root")
        self.assertEqual(command.port, 22)
        self.assertEqual(command.dynamic_forwards[0].display, "127.0.0.1:1080")

    def test_combined_flags_and_key(self) -> None:
        command = parse_ssh_command(r"ssh -p2222 -D127.0.0.1:2080 -i C:\keys\id_ed25519 admin@example.com")
        self.assertEqual(command.port, 2222)
        self.assertEqual(command.identity_file, r"C:\keys\id_ed25519")
        self.assertEqual(command.dynamic_forwards[0].bind_port, 2080)

    def test_no_command(self) -> None:
        command = parse_ssh_command("ssh -N -D 1080 -l root host.local")
        self.assertTrue(command.no_remote_command)
        self.assertEqual(command.username, "root")

    def test_multiple_dynamic_forwards(self) -> None:
        command = parse_ssh_command("ssh -D 1080 -D 127.0.0.1:1081 root@host.local")
        self.assertEqual([item.bind_port for item in command.dynamic_forwards], [1080, 1081])

    def test_server_alive_interval(self) -> None:
        command = parse_ssh_command("ssh -N -D 1080 -o ServerAliveInterval=60 root@host.local")
        self.assertTrue(command.no_remote_command)
        self.assertEqual(command.server_alive_interval, 60)

    def test_rejects_local_commands(self) -> None:
        with self.assertRaises(SSHCommandError):
            parse_ssh_command("powershell Get-Process")

    def test_rejects_unsupported_forward(self) -> None:
        with self.assertRaises(SSHCommandError):
            parse_ssh_command("ssh -L 8080:localhost:80 root@host")

    def test_quoted_remote_command(self) -> None:
        command = parse_ssh_command('ssh root@host "printf hello"')
        self.assertEqual(command.remote_command, "printf hello")


if __name__ == "__main__":
    unittest.main()
