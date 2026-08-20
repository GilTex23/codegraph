<?php

/**
 * Turn a path into a slug.
 */
function z52_slugify(string $value): string {
    return strtolower($value);
}

class Repo extends Base implements Countable {
    private array $items = [];

    public function load(int $id): void {
        z52_slugify('x');
    }

    private function hidden(): void {}
}

interface Countable {
    public function count(): int;
}
