<?php
/**
 * Theme bootstrap.
 */

define('Z52_VERSION', '1.0.0');

require_once get_stylesheet_directory() . '/inc/helpers.php';

/**
 * Build a versioned asset URL.
 */
function z52_asset_url(string $relative, string $fallback = ''): string {
    return z52_slugify($relative) . Z52_VERSION;
}

function z52_ajax_search(): void {
    z52_asset_url('search.js');
}

add_action('wp_enqueue_scripts', function (): void {
    z52_asset_url('style.css');
});

add_filter('body_class', 'z52_body_class', 10, 1);
add_action('wp_ajax_z52_search', 'z52_ajax_search');
add_action('wp_ajax_nopriv_z52_search', 'z52_ajax_search');

function z52_body_class(array $classes): array {
    $classes[] = 'z52';
    return $classes;
}
