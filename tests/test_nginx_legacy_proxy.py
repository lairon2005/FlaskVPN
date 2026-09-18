import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LegacySubscriptionRedirectTests(unittest.TestCase):
    def test_legacy_route_strips_sub_prefix_before_catch_all(self) -> None:
        template = (ROOT / "etc/nginx/templates/default.conf.template").read_text()

        legacy_location = template.index("location ~ ^/sub/(.*)$")
        catch_all_location = template.index("location / {", legacy_location)

        self.assertLess(legacy_location, catch_all_location)
        self.assertIn(
            "return 302 https://$REMNAWAVE_SUB_HOST/$1$is_args$args;",
            template,
        )

    def test_legacy_route_does_not_proxy_between_servers(self) -> None:
        template = (ROOT / "etc/nginx/templates/default.conf.template").read_text()
        location_start = template.index("location ~ ^/sub/(.*)$")
        location_end = template.index("\n    }", location_start)
        legacy_location = template[location_start:location_end]

        self.assertIn('add_header Cache-Control "no-store" always;', legacy_location)
        self.assertNotIn("proxy_pass", legacy_location)

    def test_compose_passes_subscription_host_to_nginx(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertIn(
            "REMNAWAVE_SUB_HOST: ${REMNAWAVE_SUB_HOST:-sub.flaskvpn.ru}",
            compose,
        )

    def test_refresh_renders_template_without_recreating_dependencies(self) -> None:
        refresh = (ROOT / "refresh.sh").read_text()

        render = "docker compose run --rm --no-deps -T nginx nginx -t"
        validate = "docker compose exec -T nginx nginx -t"
        reload_nginx = "docker compose exec -T nginx nginx -s reload"

        self.assertLess(refresh.index(render), refresh.index(validate))
        self.assertLess(refresh.index(validate), refresh.index(reload_nginx))
        self.assertNotIn("docker compose down", refresh)
        self.assertNotIn("force-recreate marzban", refresh)


if __name__ == "__main__":
    unittest.main()
