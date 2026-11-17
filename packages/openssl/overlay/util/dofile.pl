#!/usr/bin/perl
use strict;
use warnings;
use Text::Template;

# Skip any -I. or -Mconfigdata arguments that may be passed
while (@ARGV && ($ARGV[0] eq '-I.' || $ARGV[0] eq '-Mconfigdata')) {
    shift @ARGV;
}

# Skip the output directory flag (e.g., -oHarbour)
if (@ARGV && $ARGV[0] =~ /^-o/) {
    shift @ARGV;
}

# Skip the -i.in flag
if (@ARGV && $ARGV[0] eq '-i.in') {
    shift @ARGV;
}

# Remaining arguments are the list of .in files to process
my @files = @ARGV;

foreach my $file (@files) {
    open my $fh, '<', $file or die "Cannot open $file: $!";
    local $/;
    my $content = <$fh>;
    close $fh;

    # Create a Text::Template object for the file content
    my $template = Text::Template->new($content, 'FILE');
    my $output = $template->fill_in();

    # Write the processed content to the same path without the .in suffix
    my $out_file = $file;
    $out_file =~ s/\.in$//;
    open my $outfh, '>', $out_file or die "Cannot write $out_file: $!";
    print $outfh $output;
    close $outfh;
}
